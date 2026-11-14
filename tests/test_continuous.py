import json
import os
import signal
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop.adapters import continuing_worker_command
from loop.continuous import ContinuousRun
from loop.keeper import Scribe
from loop.worker import process_identity, worktree_activity


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class ContinuousBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "app.py").write_text("value = 1\n")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-qm", "initial")
        self.run = ContinuousRun(self.repo, "example", limits=lambda _: {},
                                 out=lambda _: None, sleep=lambda _: None, scribe=Scribe())
        self.run.create("Set value to 2", options={
            "workers": ["claude"], "acceptance": ["grep -q 'value = 2' app.py"],
            "audit": {"enabled": False}})
        state = self.run.state()
        self.run._setup(state)


class ContinuousTests(ContinuousBase):
    def test_worker_effort_is_saved_in_each_attempt(self):
        for effort in (None, "high", "low"):
            with self.subTest(effort=effort):
                config = self.run.load("config.json")
                if effort:
                    config["worker_effort"] = effort
                self.run.save("config.json", config)
                state = self.run.state()
                state.update(state="ready", attempt=None)
                self.run.save_state(state)
                with mock.patch.object(self.run, "_spawn"):
                    self.run._launch(state)
                attempt = self.run.load("attempts/" + state["attempt"] + ".json")
                self.assertEqual(attempt["task"]["effort"], effort or "medium")

    def test_resume_persists_effort_without_resetting_session(self):
        from loop.__main__ import main
        state = self.run.state()
        state["session_id"] = "existing-session"
        self.run.save_state(state)
        for flags in (["--worker-effort", "high", "--supervisor-effort", "low"], []):
            with mock.patch("sys.argv", ["loop", "--repo", str(self.repo), "lite", "resume",
                                         self.run.id, *flags]), \
                    mock.patch.object(ContinuousRun, "run", return_value={"state": "ready"}), \
                    mock.patch("builtins.print"):
                main()
            self.assertEqual(self.run.config["worker_effort"], "high")
            self.assertEqual(self.run.config["supervisor_effort"], "low")
            self.assertEqual(self.run.state()["session_id"], "existing-session")

    def test_objective_reviewer_uses_selected_supervisor_effort(self):
        config = self.run.load("config.json")
        config.update(review_provider="claude", supervisor_effort="low")
        self.run.save("config.json", config)
        response = json.dumps({"type": "result", "is_error": False,
                               "result": '{"verdict":"pass"}'})
        with mock.patch.object(self.run, "invoke", return_value=(0, response, "")) as invoke:
            self.assertTrue(self.run._review(self.run.state(), ["app.py"]))
        argv = invoke.call_args.args[0]
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_failed_check_repairs_in_same_session_and_preserves_edits(self):
        state = self.run.state()
        (self.run.worktree / "app.py").write_text("value = 3\n")
        state["attempt"] = "first"
        self.run._consume(state, {"status": "ok", "response": "still working",
                                  "session_id": "session-one", "usage": {}})
        state = self.run.state()
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["session_id"], "session-one")
        self.assertEqual((self.run.worktree / "app.py").read_text(), "value = 3\n")
        (self.run.worktree / "app.py").write_text("value = 2\n")
        state["attempt"] = "second"
        self.run._consume(state, {"status": "ok", "response": "done",
                                  "session_id": "session-one", "usage": {}})
        self.assertEqual(self.run.state()["state"], "complete")
        self.assertEqual(git(self.run.worktree, "show", "--format=%s", "-s", "HEAD"),
                         "lite: complete example attempt second")

    def test_reworded_attempts_do_not_reset_failure_bound(self):
        state = self.run.state()
        (self.run.worktree / "app.py").write_text("value = 3\n")
        for index in range(5):
            state["attempt"] = "attempt-%d" % index
            self.run._consume(state, {"status": "ok", "response": "different prompt %d" % index,
                                      "usage": {}})
            state = self.run.state()
        self.assertEqual(state["state"], "waiting")
        self.assertEqual(state["failure_count"], 5)
        self.assertIn("unchanged failed check", state["wait_reason"])

    def test_passing_baseline_check_cannot_finish(self):
        state = self.run.state()
        state["baseline"]["grep -q 'value = 2' app.py"]["code"] = 0
        (self.run.worktree / "app.py").write_text("value = 2\n")
        state["attempt"] = "one"
        self.run._consume(state, {"status": "ok", "response": '{"status":"complete"}',
                                  "usage": {}})
        self.assertEqual(self.run.state()["state"], "waiting")
        self.assertEqual(git(self.run.worktree, "rev-parse", "HEAD"), state["accepted_commit"])

    def test_launch_error_and_corrupt_state_suspend_safely(self):
        state = self.run.state()
        with mock.patch.object(self.run, "_spawn", side_effect=OSError("temporary")):
            self.run._launch(state)
        state = self.run.state()
        self.assertEqual(state["state"], "waiting")
        self.assertIsNone(state["attempt"])
        self.assertEqual((self.run.worktree / "app.py").read_text(), "value = 1\n")
        self.run.path("continuous.json").write_text("{")
        with self.assertRaisesRegex(RuntimeError, "unreadable"):
            self.run.state()

    def test_commit_after_state_crash_reconciles_without_second_commit(self):
        state = self.run.state()
        (self.run.worktree / "app.py").write_text("value = 2\n")
        state.update(state="commit_pending", verification_attempt="one",
                     verified_tree=self.run._tree_fingerprint())
        self.run.save_state(state)
        git(self.run.worktree, "add", "app.py")
        git(self.run.worktree, "commit", "-qm", "lite: complete example attempt one")
        head = git(self.run.worktree, "rev-parse", "HEAD")
        self.run._reconcile_commit(self.run.state())
        self.assertEqual(self.run.state()["state"], "complete")
        self.assertEqual(git(self.run.worktree, "rev-parse", "HEAD"), head)

    def test_scribe_summary_feeds_the_handoff(self):
        class Local:
            def summarise(self, goal, status, response):
                return {"handoff": "local summary"}, "ollama"
        self.run._scribe = Local()
        state = self.run.state()
        state["attempt"] = "one"
        self.run._consume(state, {"status": "ok", "response": "long text", "usage": {}})
        state = self.run.state()
        self.assertEqual(state["handoff"], "local summary")
        self.assertEqual(state["summary_source"], "ollama")

    def test_malformed_attempt_record_parks_instead_of_crashing(self):
        state = self.run.state()
        state.update(state="working", attempt="broken")
        self.run.save_state(state)
        self.run.path("attempts").mkdir(exist_ok=True)
        (self.run.path("attempts") / "broken.json").write_text('{"created_at": 1}')
        self.assertIsNone(self.run._reconcile(state))
        state = self.run.state()
        self.assertEqual(state["state"], "waiting")
        self.assertIn("malformed", state["wait_reason"])

    def test_reused_pid_is_not_mistaken_for_the_worker_group(self):
        leader = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(leader.wait)
        self.addCleanup(leader.kill)
        identity = process_identity(leader.pid)
        if not identity:
            self.skipTest("Host does not expose process start identities through ps")
        record = {"provider_pid": leader.pid, "provider_start_identity": identity}
        self.assertTrue(self.run._group_present(record))
        record["provider_start_identity"] = "Thu Jan  1 00:00:00 1970"
        self.assertFalse(self.run._group_present(record))

    def test_group_identity_mismatch_and_unavailable_identity(self):
        record = {"provider_pid": 12345, "provider_start_identity": "original-start"}
        with mock.patch("loop.continuous.process_identity", return_value="different-start"), \
                mock.patch("loop.continuous.os.killpg") as killpg:
            self.assertFalse(self.run._group_present(record))
            killpg.assert_not_called()
        # Missing host metadata must keep the ownership check conservative.
        with mock.patch("loop.continuous.process_identity", return_value=""), \
                mock.patch("loop.continuous.os.killpg") as killpg:
            self.assertTrue(self.run._group_present(record))
            killpg.assert_called_once_with(12345, 0)

    def test_symlink_escaping_the_worktree_is_out_of_scope(self):
        os.symlink("/etc", self.run.worktree / "escape")
        state = self.run.state()
        self.assertFalse(self.run._verify(state))
        self.assertIn("out-of-scope", self.run.state()["wait_reason"])

    def test_continuing_command_shapes(self):
        fresh = continuing_worker_command("codex", "P", "m", "medium", None, "/w")
        self.assertEqual(fresh[:2], ["codex", "exec"])
        self.assertNotIn("--ephemeral", fresh)
        self.assertIn("-s", fresh)
        resumed = continuing_worker_command("codex", "P", "m", "medium", "SID", "/w")
        self.assertEqual(resumed[:3], ["codex", "exec", "resume"])
        self.assertNotIn("-s", resumed)
        self.assertNotIn("--ephemeral", resumed)
        self.assertIn('sandbox_mode="workspace-write"', resumed)
        self.assertEqual(resumed[-2], "SID")
        self.assertTrue(resumed[-1].startswith("P"))
        claude = continuing_worker_command("claude", "P", None, "medium", "SID", "/w")
        self.assertNotIn("--no-session-persistence", claude)
        self.assertEqual(claude[-2:], ["--resume", "SID"])
        self.assertEqual(claude[claude.index("--effort") + 1], "medium")
        agy = continuing_worker_command("antigravity", "P", None, None, "SID", "/w")
        self.assertEqual(agy[-2:], ["--conversation", "SID"])
        muse = continuing_worker_command("muse", "P", "m", "medium", "SID", "/w")
        self.assertEqual(muse[:2], ["muse", "exec"])
        self.assertEqual(muse[-3:-1], ["--session-id", "SID"])
        self.assertTrue(muse[-1].startswith("P"))

    def test_worktree_activity_ignores_git_and_dependency_trees(self):
        before = worktree_activity(self.run.worktree)
        (self.run.worktree / "node_modules").mkdir()
        (self.run.worktree / "node_modules" / "x.js").write_text("x")
        self.assertEqual(worktree_activity(self.run.worktree), before)
        (self.run.worktree / "new.py").write_text("y")
        self.assertNotEqual(worktree_activity(self.run.worktree), before)

    def test_straggler_holding_the_lock_after_result_is_reaped(self):
        from loop.worker import workspace_lock_path
        lock = workspace_lock_path(self.run.dir, self.run.worktree)
        lock.parent.mkdir(parents=True, exist_ok=True)
        straggler = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,sys,time;h=open(sys.argv[1],'w');"
             "fcntl.flock(h,fcntl.LOCK_EX);print('locked',flush=True);time.sleep(60)", str(lock)],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(straggler.wait)
        self.addCleanup(straggler.kill)
        self.assertEqual(straggler.stdout.readline().strip(), "locked")
        record = {"attempt_id": "a1", "nonce": "n", "run_id": self.run.id,
                  "base": "b", "created_at": time.time(), "state": "result_written",
                  "result_path": str(self.run.path("results") / "a1.json")}
        self.run.path("attempts").mkdir(exist_ok=True)
        self.run.path("results").mkdir(exist_ok=True)
        (self.run.path("attempts") / "a1.json").write_text(json.dumps(record))
        (self.run.path("results") / "a1.json").write_text(json.dumps({
            "attempt_id": "a1", "nonce": "n", "run_id": self.run.id, "base": "b",
            "result": {"status": "ok", "response": "done"}}))
        state = self.run.state()
        state["attempt"] = "a1"
        payload = self.run._reconcile(state)
        self.assertEqual(payload["status"], "ok")
        straggler.wait(timeout=5)
        self.assertFalse(self.run._owner_present())


class DetachedProcessTests(ContinuousBase):
    def setUp(self):
        super().setUp()
        binaries = Path(self.repo).parent / "bin"
        binaries.mkdir()
        shim = binaries / "codex"
        shim.write_text('#!/bin/sh\nexec %s %s\n' % (
            sys.executable, Path(__file__).resolve().parent / "fake_provider.py"))
        shim.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "FAKE_PROVIDER_FILE": "app.py", "FAKE_PROVIDER_CONTENT": "value = 2\n"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run.save_state({**self.run.state(), "provider": "codex"})
        self.processes = []
        spawn = self.run._spawn
        self.run._spawn = lambda path: self.processes.append(spawn(path)) or self.processes[-1]
        self.addCleanup(self.reap)

    def reap(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=10)

    def test_full_run_with_real_provider_process_completes(self):
        state = self.run.run()
        self.assertEqual(state["state"], "complete")
        self.assertEqual((self.run.worktree / "app.py").read_text(), "value = 2\n")
        self.assertEqual(git(self.run.worktree, "show", "--format=%s", "-s", "HEAD")[:26],
                         "lite: complete example att")

    def test_killed_wrapper_keeps_ownership_while_provider_lives(self):
        os.environ["FAKE_PROVIDER_HOLD_SECONDS"] = "60"
        self.addCleanup(os.environ.pop, "FAKE_PROVIDER_HOLD_SECONDS", None)
        state = self.run.state()
        self.run._launch(state)
        state = self.run.state()
        record = {}
        for _ in range(100):
            record = json.loads(self.run._attempt_path(state).read_text())
            if record.get("provider_pid"):
                break
            time.sleep(0.1)
        self.assertTrue(record.get("provider_pid"))
        os.kill(record["pid"], signal.SIGKILL)
        time.sleep(0.3)
        self.assertTrue(self.run._owner_present())
        self.assertIsNone(self.run._reconcile(state))
        self.assertEqual(self.run.state()["state"], "working")
        os.killpg(record["provider_pid"], signal.SIGKILL)
        for _ in range(100):
            if not self.run._owner_present() and not self.run._group_present(record):
                break
            time.sleep(0.1)
        payload = self.run._reconcile(self.run.state())
        self.assertEqual(payload["status"], "error")
        self.assertIn("without a result", payload["error"])


if __name__ == "__main__":
    unittest.main()
