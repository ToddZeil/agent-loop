"""Opt-in Lite workflow with one continuing coding worker.

Control records live outside the editable worktree. A detached worker owns each
provider turn; the keeper reconciles its lock and durable result before it can
launch another turn or accept a change.
"""
import fcntl
from contextlib import ExitStack
from functools import wraps
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path

from .adapters import (Result, context_tokens, parse, quota_deadline,
                       supervisor_command, token_total)
from .lite import DEFAULTS, LiteRun, changed_files, extract_verdict, git, tail
from .storage import atomic_json
from .usage import record as record_usage
from .worker import process_identity, workspace_lock_path, legacy_workspace_lock_path, terminal_owner_path

VERSION = 1


def worktree_admitted(method):
    """Hold admission across a keeper operation that can touch the worktree."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        local = getattr(self, '_admission_local', None)
        if local is None:
            local = self._admission_local = threading.local()
        if getattr(local, 'held', False):
            return method(self, *args, **kwargs)
        with ExitStack() as stack:
            paths = (workspace_lock_path(self.dir, self.worktree),
                     legacy_workspace_lock_path(self.dir, self.worktree))
            handles = []
            for path in dict.fromkeys(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = stack.enter_context(path.open('a'))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handles.append(handle)
            if terminal_owner_path(self.dir, self.worktree).exists():
                raise BlockingIOError('Terminal ownership requires reconciliation')
            local.held = True
            local.fds = tuple(handle.fileno() for handle in handles)
            try:
                return method(self, *args, **kwargs)
            finally:
                local.held = False
                local.fds = ()
    return guarded


def failure_key(command, code, output):
    """Group the same check failure independent of worker prompt wording."""
    normalized = re.sub(r"\b\d+(?:\.\d+)?s\b", "<time>", str(output).lower())
    normalized = re.sub(r"0x[0-9a-f]+", "<address>", normalized)
    normalized = re.sub(r"\bline \d+\b", "line <n>", normalized)
    normalized = "\n".join(line.strip() for line in normalized.splitlines() if line.strip())
    return hashlib.sha256(json.dumps([command, code, normalized[-12000:]]).encode()).hexdigest()


CROSS_REPO_NOTICE = (" The operator allowed cross-repository work for this agent: you may read and edit "
                     "other repositories on this machine when the objective needs them. Name every "
                     "repository you changed in your reply.")


class ContinuousRun(LiteRun):
    def state(self):
        path = self.path("continuous.json")
        if not path.exists():
            raise ValueError("No continuous state for run " + self.id)
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError("Continuous state is unreadable; dispatch suspended: " + str(exc)) from exc
        if not isinstance(value, dict) or value.get("version") != VERSION:
            raise RuntimeError("Unsupported continuous state; dispatch suspended")
        return value

    def save_state(self, state):
        atomic_json(self.path("continuous.json"), state)

    def create(self, goal, base="HEAD", options=None):
        options = dict(options or {})
        if not options.get("acceptance") and not options.get("review_provider"):
            raise ValueError("Continuous runs require an objective acceptance check or reviewer")
        if self.path("goal.md").exists():
            raise ValueError("Run already exists: " + self.id)
        self.ensure_excluded()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path("logs").mkdir(exist_ok=True)
        base_sha = git(self.repo, "rev-parse", base + "^{commit}")
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-b", self.branch, str(self.worktree), base_sha)
        self.path("goal.md").write_text(goal.strip() + "\n")
        config = {**DEFAULTS, **options, "workflow": "continuous"}
        self.save("config.json", config)
        state = {"version": VERSION, "state": "ready", "base": base_sha,
                 "accepted_commit": base_sha, "provider": (config.get("workers") or ["codex"])[0],
                 "model": options.get("worker_model"), "session_id": None,
                 "sessions": {}, "context_tokens": 0,
                 "attempt": None, "turns": 0, "check_evidence": [], "failures": [],
                 "failure_key": None, "failure_count": 0, "recovery_used": False,
                 "progress": [], "checklist": [], "handoff": "", "tokens": {}, "cost": {},
                 "wait_reason": "", "retry_at": 0, "next_action": "launch worker",
                 "created_at": self.clock(), "completed_at": None, "setup_done": False,
                 "baseline": {}, "operator_notes": []}
        self.save_state(state)
        self.journal("created", workflow="continuous", base=base_sha,
                     worktree=str(self.worktree))
        return state

    def _check(self, command_text):
        try:
            code, out, err = self.invoke(["/bin/sh", "-c", command_text], self.worktree,
                                         self.config["check_timeout_seconds"])
            return {"command": command_text, "code": code, "output": tail(out + "\n" + err, 100, 12000)}
        except (OSError, ValueError) as exc:
            return {"command": command_text, "code": 127, "output": str(exc)}

    def _tree_fingerprint(self):
        digest = hashlib.sha256()
        for name in changed_files(self.worktree):
            path = self.worktree / name
            digest.update(name.encode() + b"\0")
            if path.is_file():
                digest.update(path.read_bytes())
            else:
                digest.update(b"<deleted>")
        return digest.hexdigest()

    @worktree_admitted
    def _setup(self, state):
        if state["setup_done"]:
            return
        for cmd in self.config.get("setup", []):
            result = self._check(cmd)
            if result["code"]:
                self._wait(state, "setup failed: " + result["output"][-500:],
                           self.clock() + self.config["unknown_retry_seconds"])
                return
        state["setup_done"] = True
        before = self._tree_fingerprint()
        state["baseline"] = {cmd: self._check(cmd) for cmd in self.config.get("acceptance", [])}
        if self._tree_fingerprint() != before:
            self._wait(state, "baseline acceptance check modified the worktree")
            return
        self.save_state(state)

    def _wait(self, state, reason, retry_at=0):
        state.update(state="waiting", wait_reason=reason, retry_at=retry_at,
                     next_action=("retry after %.0f" % retry_at) if retry_at else "operator instruction")
        self.save_state(state)
        self.journal("waiting", reason=reason[:500], retry_at=retry_at)

    def _owner_present(self):
        path = workspace_lock_path(self.dir, self.worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            legacy_path = legacy_workspace_lock_path(self.dir, self.worktree)
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            if legacy_path != path:
                with legacy_path.open('a') as legacy:
                    try:
                        fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
            if terminal_owner_path(self.dir, self.worktree).exists():
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def _group_present(self, record):
        pid = record.get("provider_pid")
        if not pid:
            return False
        recorded = record.get("provider_start_identity")
        current = process_identity(pid)
        if recorded and current and current != recorded:
            return False  # the PID now belongs to an unrelated process
        try:
            os.killpg(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _reap_stragglers(self, record):
        """Stop the recorded worker group without signalling unknown owners."""
        if terminal_owner_path(self.dir, self.worktree).exists():
            return  # Never kill holders belonging to an uncertain terminal owner.
        pid = record.get("provider_pid")
        recorded = record.get("provider_start_identity")
        current = process_identity(pid) if pid else ""
        if pid and not (recorded and current and current != recorded):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        # A shared lock holder can belong to another supervisor. Do not signal
        # arbitrary lsof results; an unidentified holder remains fail-closed.
        for _ in range(20):
            if not self._owner_present():
                return
            time.sleep(0.1)

    def _attempt_path(self, state):
        return self.path("attempts") / (state["attempt"] + ".json")

    def _reconcile(self, state):
        """Return a finished payload, None while owned, or an error payload."""
        path = self._attempt_path(state)
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            self._wait(state, "attempt record missing or corrupt: " + str(exc))
            return None
        required = ("attempt_id", "nonce", "run_id", "base", "result_path", "created_at")
        if not isinstance(record, dict) or any(key not in record for key in required):
            self._wait(state, "attempt record malformed; inspect " + str(path))
            return None
        result_path = Path(record["result_path"])
        owned = self._owner_present() or self._group_present(record)
        if owned and record.get("state") == "result_written" and result_path.exists():
            self._reap_stragglers(record)
            owned = self._owner_present() or self._group_present(record)
        if owned:
            state.update(state="working", next_action="wait for owned worker")
            self.save_state(state)
            return None
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
            except (OSError, ValueError) as exc:
                self._wait(state, "worker result unreadable: " + str(exc))
                return None
            for key in ("attempt_id", "nonce", "run_id", "base"):
                if result.get(key) != record.get(key):
                    self._wait(state, "worker result identity mismatch")
                    return None
            return result.get("result") or {"status": "error", "error": "empty worker result"}
        if record.get("state") == "intended" and self.clock() - record["created_at"] < 30:
            return None
        return {"status": "error", "error": "worker exited without a result", "invoked": False}

    def _prompt(self, state):
        goal = self.path("goal.md").read_text()
        scope = self.config.get("scope") or ["."]
        protected = self.config.get("protected") or []
        recent = state["check_evidence"][-2:]
        parts = ["OBJECTIVE\n" + goal,
                 "Allowed repository roots: " + json.dumps(scope)
                 + (CROSS_REPO_NOTICE if self.config.get("cross_repo") else ""),
                 "Protected paths: " + json.dumps(protected),
                 "Objective acceptance commands (keeper owned): " + json.dumps(self.config.get("acceptance", [])),
                 "Keep a short checklist in your reply; continue implementation and run focused checks.",
                 "Current checklist: " + json.dumps(state["checklist"]),
                 "Previous evidence: " + json.dumps(recent),
                 "Handoff: " + state["handoff"],
                 "Operator notes: " + json.dumps(state["operator_notes"][-5:])]
        if self.repo == Path(__file__).resolve().parent.parent:
            helper = Path(__file__).resolve().parent.parent / "scripts" / "merge-deploy"
            parts.append("To publish this repository's completed work to main and redeploy its local workspace, "
                         "run " + str(helper) + " from the worktree when the user requests deployment. "
                         "The helper snapshots edits without committing or switching this worker branch, "
                         "runs verification, merges to main and restores worker chats. Do not start a separate preview "
                         "as a substitute for deploying the primary workspace.")
        if state["failure_count"] >= 2:
            parts.append("Repeated unchanged check failure. Diagnose the exact cause and change strategy once.")
        return "\n\n".join(parts)

    def _adopt_worker_git_history(self, state):
        """With allow_git, accept the HEAD a worker left instead of treating it as foreign."""
        if not self.config.get("allow_git"):
            return
        head = git(self.worktree, "rev-parse", "HEAD")
        if head != state["accepted_commit"]:
            self.journal("git_history_adopted", old=state["accepted_commit"], new=head,
                         branch=git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"))
            state["accepted_commit"] = head
            self.save_state(state)

    def _launch(self, state):
        if self._owner_present():
            self._wait(state, "workspace ownership uncertain; existing worker lock is held")
            return
        self._adopt_worker_git_history(state)
        if git(self.worktree, "rev-parse", "HEAD") != state["accepted_commit"]:
            self._wait(state, "unexpected Git history; inspect worktree before resuming")
            return
        attempt_id = uuid.uuid4().hex
        record = {"continuous": True, "attempt_id": attempt_id, "nonce": uuid.uuid4().hex,
                  "run_id": self.id, "task_id": "objective", "base": state["accepted_commit"],
                  "policy_version": VERSION, "created_at": self.clock(), "state": "intended",
                  "workspace": str(self.worktree), "home": str(self.dir),
                  "result_path": str(self.path("results") / (attempt_id + ".json")),
                  "log_directory": str(self.path("logs")), "prompt": self._prompt(state),
                  "session_id": state["session_id"], "worker_idle_timeout_seconds": self.config["worker_idle_seconds"],
                  "worker_max_seconds": self.config["worker_max_seconds"],
                  "task": {"provider": state["provider"], "model": state["model"], "effort": (self.config.get("worker_effort") or "medium"),
                           "full_access": self.config.get("full_access", True),
                           "allow_subagents": self.config.get("allow_subagents", True),
                           "allow_git": bool(self.config.get("allow_git"))}}
        path = self.path("attempts") / (attempt_id + ".json")
        atomic_json(path, record)
        state.update(state="working", attempt=attempt_id, next_action="reconcile worker")
        self.save_state(state)
        self.journal("launch_intent", attempt=attempt_id, provider=state["provider"])
        try:
            self._spawn(path)
        except OSError as exc:
            self.journal("launch_error", error=str(exc))
            state["attempt"] = None
            self._wait(state, "temporary worker launch error: " + str(exc),
                       self.clock() + self.config["unknown_retry_seconds"])

    def _spawn(self, path):
        return subprocess.Popen([sys.executable, "-m", "loop.worker", str(path)],
                                cwd=Path(__file__).resolve().parent.parent,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    @worktree_admitted
    def _verify(self, state):
        files = changed_files(self.worktree)
        self._adopt_worker_git_history(state)
        if git(self.worktree, "rev-parse", "HEAD") != state["accepted_commit"]:
            self._wait(state, "unexpected Git history during verification")
            return False
        scope = self.config.get("scope") or ["."]
        protected = self.config.get("protected") or []
        def allowed(name):
            if name == ".git" or name.startswith(".git/") or name.startswith(".agent-loop/"):
                return False
            if any(name == item or name.startswith(item.rstrip("/") + "/") for item in protected):
                return False
            return any(root == "." or name == root or name.startswith(root.rstrip("/") + "/")
                       for root in scope)
        root = self.worktree.resolve()

        def escapes(name):
            path = self.worktree / name
            return path.is_symlink() and not path.resolve().is_relative_to(root)
        files_out = [name for name in files if not allowed(name) or escapes(name)]
        if files_out:
            self._wait(state, "out-of-scope edits: " + ", ".join(files_out))
            return False
        state["state"] = "verifying"
        self.save_state(state)
        before = self._tree_fingerprint()
        checks = []
        commands = list(dict.fromkeys(self.config.get("acceptance", []) + self.config.get("gates", [])))
        for cmd in commands:
            result = self._check(cmd)
            checks.append(result)
            if result["code"]:
                key = failure_key(cmd, result["code"], result["output"])
                if key == state["failure_key"]:
                    state["failure_count"] += 1
                else:
                    state.update(failure_key=key, failure_count=1, recovery_used=False)
                state["failures"].append({"at": self.clock(), "key": key, **result})
                state["check_evidence"] = checks
                if state["failure_count"] >= 3 and state["recovery_used"]:
                    self._wait(state, "unchanged failed check after diagnostic recovery: " + result["output"][-500:])
                else:
                    if state["failure_count"] >= 2:
                        state["recovery_used"] = True
                    state.update(state="ready", next_action="repair failed check")
                    self.save_state(state)
                return False
        state["check_evidence"] = checks
        baseline = state.get("baseline") or {}
        objective_evidence = any(baseline.get(cmd, {}).get("code") and result["code"] == 0
                                 for result in checks for cmd in [result["command"]]
                                 if cmd in self.config.get("acceptance", []))
        if not objective_evidence:
            if not self._review(state, files):
                return False
        committed_by_worker = bool(self.config.get("allow_git")) and state["accepted_commit"] != state["base"]
        if not files and not committed_by_worker:
            self._wait(state, "acceptance passed without a delivered worktree change")
            return False
        if self._tree_fingerprint() != before or git(self.worktree, "rev-parse", "HEAD") != state["accepted_commit"]:
            self._wait(state, "verification changed worktree content or Git history")
            return False
        if not files:
            state.update(state="complete", completed_at=self.clock(), next_action="none", wait_reason="")
            state["progress"].append({"at": self.clock(), "kind": "objective acceptance passed",
                                      "checks": checks, "commit": state["accepted_commit"]})
            self.save_state(state)
            self.journal("complete", commit=state["accepted_commit"], committed_by="worker")
            return True
        state.update(state="commit_pending", verified_tree=before)
        self.save_state(state)
        inherited_locks = getattr(getattr(self, '_admission_local', None), 'fds', ())
        subprocess.check_call(["git", "-C", str(self.worktree), "add", "--", *files], pass_fds=inherited_locks)
        subprocess.check_call(["git", "-C", str(self.worktree), "commit", "-m",
                               "lite: complete " + self.id + " attempt " + state["verification_attempt"]],
                              pass_fds=inherited_locks)
        state.update(state="complete", accepted_commit=git(self.worktree, "rev-parse", "HEAD"),
                     completed_at=self.clock(), next_action="none", wait_reason="")
        state["progress"].append({"at": self.clock(), "kind": "objective acceptance passed",
                                  "checks": checks, "commit": state["accepted_commit"]})
        self.save_state(state)
        self.journal("complete", commit=state["accepted_commit"])
        return True

    def _review(self, state, files):
        provider = self.config.get("review_provider")
        if not provider:
            self._wait(state, "acceptance passed at baseline or is absent; objective review required")
            return False
        sections = []
        for name in files:
            target = self.worktree / name
            if target.is_file():
                sections.append(name + "\n" + target.read_text(errors="replace"))
            else:
                sections.append(name + " <deleted>")
        content = "\n\n".join(sections)
        if len(content) > 100000:
            self._wait(state, "review input exceeds 100000 characters; add substantive acceptance checks")
            return False
        prompt = ("Read-only objective review. Inspect changed worktree files and the full content below. "
                  "Judge whether the requested objective was delivered substantively. "
                  "Reply JSON {\"verdict\":\"pass|fail\",\"problems\":[...]}.\n\n"
                  "GOAL\n" + self.path("goal.md").read_text() + "\nFILES\n" + content)
        before = self._tree_fingerprint()
        try:
            argv = supervisor_command(provider, prompt, effort=self.config["supervisor_effort"], workspace=self.worktree)
            code, out, err = self.invoke(argv, self.worktree, self.config["supervisor_max_seconds"],
                                         idle_timeout=self.config["supervisor_idle_seconds"])
            result = parse(provider, code, out, err, now=self.clock())
        except (OSError, ValueError) as exc:
            self._wait(state, "objective reviewer unavailable: " + str(exc),
                       self.clock() + self.config["unknown_retry_seconds"])
            return False
        record_usage(state, "reviewer", provider, result.usage, "reviewer:" + uuid.uuid4().hex,
                     now=self.clock())
        self.account(state, "reviewer", provider, result)
        if self._tree_fingerprint() != before:
            self._wait(state, "reviewer changed worktree content")
            return False
        verdict = extract_verdict(result.response) if result.status == "ok" else None
        if not verdict:
            self._wait(state, "objective reviewer unavailable: " + (result.error or err)[-500:],
                       result.retry_at or self.clock() + self.config["unknown_retry_seconds"])
            return False
        state["review_evidence"] = {"tree": before, "provider": provider, "verdict": verdict}
        self.save_state(state)
        if verdict["verdict"] != "pass":
            self._wait(state, "objective review failed: " + json.dumps(verdict.get("problems", []))[-500:])
            return False
        return True

    @worktree_admitted
    def _consume(self, state, payload):
        attempt = state["attempt"]
        state["attempt"] = None
        state["verification_attempt"] = attempt
        state["turns"] += 1
        provider = state["provider"]
        result = Result(payload.get("status", "error"), payload.get("response", ""),
                        payload.get("usage") or {}, payload.get("retry_at") or 0,
                        payload.get("error") or "", payload.get("estimated_usd") or 0,
                        bool(payload.get("invoked", True)))
        record_usage(state, "worker", provider, result.usage, "worker:" + str(attempt),
                     model=state.get("model"), now=self.clock())
        self.account(state, "worker", provider, result)
        state["tokens"][provider] = state["tokens"].get(provider, 0) + token_total(
            provider, result.usage)
        if payload.get("session_id"):
            state["session_id"] = payload["session_id"]
            state["sessions"][provider] = payload["session_id"]
        state["context_tokens"] = context_tokens(provider, result.usage)
        if state["context_tokens"] >= self.config["reset_at_tokens"]:
            state["session_id"] = None
            state["sessions"].pop(provider, None)
            state["context_tokens"] = 0
        response = result.response.strip()
        try:
            summary, source = self.scribe.summarise("objective", result.status, response)
            state["handoff"] = str(summary.get("handoff") or tail(response, 60, 5000))[:5000]
            state["summary_source"] = source
        except Exception:
            state["handoff"] = tail(response, 60, 5000)
            state["summary_source"] = "fallback"
        try:
            parsed = json.loads(response)
            if isinstance(parsed, dict) and isinstance(parsed.get("checklist"), list):
                state["checklist"] = [str(item)[:300] for item in parsed["checklist"][:30]]
        except ValueError:
            pass
        self.journal("worker_result", attempt=attempt, status=result.status,
                     session=bool(state["session_id"]))
        state["last_provider_status"] = result.status
        if result.status in ("rate_limited", "provider_wait", "auth_required"):
            if self._switch_provider(state):
                return
            deadline = result.retry_at or self.clock() + self.config["unknown_retry_seconds"]
            if result.status == "auth_required":
                deadline = 0
            self._wait(state, result.error or result.status, deadline)
            return
        if result.status != "ok":
            if state["session_id"] and "session" in result.error.lower():
                state["session_id"] = None
                state["state"] = "ready"
                state["next_action"] = "fresh session from handoff"
                self.save_state(state)
                return
            self._wait(state, result.error or result.status,
                       self.clock() + self.config["unknown_retry_seconds"])
            return
        state["state"] = "verifying"
        self.save_state(state)
        self._verify(state)

    def _switch_provider(self, state):
        for candidate in self.config.get("workers", []):
            if candidate != state["provider"]:
                try:
                    limits = self.limits(candidate)
                    if quota_deadline(limits, self.clock()) > self.clock():
                        continue
                except Exception:
                    pass
                state["provider"] = candidate
                state["session_id"] = state["sessions"].get(candidate)
                state["model"] = None
                state.update(state="ready", next_action="continue with fallback provider")
                self.save_state(state)
                return True
        return False

    @worktree_admitted
    def _reconcile_commit(self, state):
        head = git(self.worktree, "rev-parse", "HEAD")
        if head == state["accepted_commit"]:
            if self._tree_fingerprint() != state.get("verified_tree"):
                self._wait(state, "verified tree changed before commit")
            else:
                self._verify(state)
            return
        parent = git(self.worktree, "rev-parse", "HEAD^")
        subject = git(self.worktree, "log", "-1", "--format=%s")
        expected = "lite: complete " + self.id + " attempt " + state["verification_attempt"]
        if parent != state["accepted_commit"] or subject != expected:
            self._wait(state, "unexpected Git history after verification")
            return
        state.update(state="complete", accepted_commit=head, completed_at=self.clock(),
                     next_action="none", wait_reason="")
        state["progress"].append({"at": self.clock(), "kind": "recovered accepted commit",
                                  "checks": state["check_evidence"], "commit": head})
        self.save_state(state)

    def run(self, max_turns=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path("keeper.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another keeper is already running for " + self.id)
            if terminal_owner_path(self.dir, self.worktree).exists():
                raise RuntimeError('Terminal ownership requires reconciliation before this worktree can be used')
            state = self.state()
            turns = 0
            while state["state"] != "complete":
                if max_turns is not None and turns >= max_turns:
                    break
                try:
                    if state["state"] == "commit_pending":
                        self._reconcile_commit(state)
                    elif state["state"] == "verifying" and not state["attempt"]:
                        self._verify(state)
                    elif state["attempt"]:
                        payload = self._reconcile(state)
                        if payload is None:
                            self.sleep(0.2)
                            state = self.state()
                            continue
                        self._consume(state, payload)
                        turns += 1
                    elif state["state"] == "waiting":
                        self._read_notes(state)
                        if state["state"] != "waiting":
                            continue
                        if not state["retry_at"]:
                            if max_turns is not None:
                                break
                            self.sleep(5)
                            state = self.state()
                            continue
                        if self.clock() < state["retry_at"]:
                            if max_turns is not None:
                                break
                            self.sleep(min(30, state["retry_at"] - self.clock()))
                            state = self.state()
                            continue
                        if state.get("last_provider_status") in ("rate_limited", "provider_wait"):
                            try:
                                limits = self.limits(state["provider"])
                                deadline = quota_deadline(limits, self.clock())
                                if deadline or not limits:
                                    self._wait(state, "provider quota still unavailable",
                                               deadline or self.clock() + self.config["unknown_retry_seconds"])
                                    state = self.state()
                                    continue
                            except Exception:
                                self._wait(state, "provider availability probe failed",
                                           self.clock() + self.config["unknown_retry_seconds"])
                                state = self.state()
                                continue
                        state.update(state="ready", wait_reason="", retry_at=0)
                        self.save_state(state)
                    else:
                        self._read_notes(state)
                        self._setup(state)
                        if state["state"] != "waiting":
                            self._launch(state)
                    state = self.state()
                except BlockingIOError:
                    if max_turns is not None:
                        break
                    self.sleep(0.2)
                    state = self.state()
                except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                    self._wait(state, "recoverable keeper error: " + str(exc),
                               self.clock() + self.config["unknown_retry_seconds"])
                    state = self.state()
            return state

    def _read_notes(self, state):
        inbox = self.path("inbox.jsonl")
        if not inbox.exists():
            return
        lines = inbox.read_text().splitlines()
        seen = state.get("inbox_seen", 0)
        for line in lines[seen:]:
            try:
                note = json.loads(line).get("text", "")
            except ValueError:
                continue
            state["operator_notes"].append(str(note)[:2000])
        if len(lines) > seen:
            state["inbox_seen"] = len(lines)
            if state["state"] == "waiting" and not state["retry_at"]:
                state.update(state="ready", wait_reason="", next_action="operator-directed retry")
            self.save_state(state)

    def status(self):
        state = self.state()
        return ("run %s  branch %s  %s\nworker: %s  session: %s  turns: %d\n"
                "progress: %d verified milestones  failures: %d\nwait: %s\nnext: %s\n"
                "accepted commit: %s" %
                (self.id, self.branch, state["state"], state["provider"],
                 state["session_id"] or "fresh", state["turns"], len(state["progress"]),
                 len(state["failures"]), state["wait_reason"] or "none",
                 state["next_action"], state["accepted_commit"]))
