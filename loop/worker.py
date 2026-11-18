"""Detached worker boundary: prompt assembly, ownership, and durable results.

The supervisor never builds a provider command line or interprets a transcript.
It writes a validated *attempt record* and launches this module as its own
session leader; everything after that — prompt assembly, provider argv, the
quota-aware runner, and the final result — belongs to the worker.

Ownership is proven by an exclusive flock on the workspace that is held from
before the provider starts until after the result is durably written. A PID, or
``kill -0`` on one, proves nothing: PIDs are reused, and a live PID says nothing
about whether that process still owns this workspace. So recovery asks the lock,
not the process table, and uses the recorded process-start identity only to
refuse a reused PID.

Because the lock is held across the provider call, a supervisor that cannot take
it must treat ownership as *unknown* and enter recovery rather than dispatching
a replacement worker into the same worktree.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .adapters import (command, continuing_worker_command, parse, runner_command,
                       run_process, session_id)
from .storage import atomic_json

HEARTBEAT_SECONDS = 5


def worktree_activity(workspace):
    """Cheap idle signal; ignored dependency and Git trees are excluded."""
    latest = (0, 0)
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [name for name in dirs if name not in
                   (".git", ".agent-loop", "node_modules", ".venv", "__pycache__")]
        for name in files:
            try:
                stat = (Path(root) / name).stat()
                latest = max(latest, (stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    return latest


def workspace_lock_path(home, workspace):
    """A lock shared by all supervisor homes for this exact worktree.

    Store it in Git's per-worktree metadata when available, outside tracked
    files. Callers also take the legacy home-local lock during migration.
    """
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        # Checking ownership must not recreate a missing worktree.
        return legacy_workspace_lock_path(home, workspace)
    git = workspace / '.git'
    if git.is_file():
        text = git.read_text().strip()
        if not text.startswith('gitdir: '):
            raise ValueError('Invalid worktree Git metadata')
        gitdir = Path(text[len('gitdir: '):])
        if not gitdir.is_absolute():
            gitdir = workspace / gitdir
        lock_root = gitdir.resolve() / 'agent-loop' / 'locks'
    elif git.is_dir():
        lock_root = git.resolve() / 'agent-loop' / 'locks'
    else:
        # Non-Git workspaces are used by the engine and isolated tests.
        lock_root = workspace / '.agent-loop' / 'locks'
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
    return lock_root / ("ws-" + digest + ".lock")


def legacy_workspace_lock_path(home, workspace):
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]
    return Path(home) / 'locks' / ('ws-' + digest + '.lock')


def _lock_with_retry(handle, timeout=1):
    """Tolerate brief ownership probes without waiting indefinitely on a writer."""
    import fcntl
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def terminal_owner_path(home, workspace):
    """Persistent admission barrier, checked while holding the workspace lock."""
    return workspace_lock_path(home, workspace).with_suffix('.terminal-owner.json')


def require_unreserved_workspace(home, workspace):
    # Even malformed or partially written records fail closed. Callers must hold
    # the corresponding workspace lock to serialize this check with admission.
    if terminal_owner_path(home, workspace).exists():
        raise RuntimeError('Terminal ownership requires reconciliation before this worktree can be used')


def reserve_terminal_workspace(home, workspace, owner):
    """Persist terminal intent before launch, with the workspace lock held.

    Never replace an existing record, including an incomplete one. A crash while
    writing leaves the file in place so later admissions fail closed.
    """
    if not isinstance(owner, dict) or not isinstance(owner.get('id'), str) or not owner['id']:
        raise ValueError('Terminal owner needs a unique identity')
    path = terminal_owner_path(home, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(owner, stream)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def process_identity(pid):
    """A start time that distinguishes this process from a later reused PID."""
    try:
        out = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True,
                                      stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return ""
    return out.strip()


def build_prompt(record):
    """Assemble the worker prompt from the validated task record alone.

    Instructions come first, then the failure being repaired, then journal notes,
    then context excerpts: when the budget binds, the worker keeps what it needs
    to act and loses only breadth.
    """
    from .context import DEFAULT_CONTEXT_CHARACTERS, fit
    task = record["task"]
    workspace = Path(record["workspace"])
    os.environ["AGENT_LOOP_DEPLOY_COMMAND"] = str(Path(__file__).resolve().parent.parent / "scripts" / "merge-deploy")
    sections = [("instructions",
                 "Implement this coding section directly in the managed worktree. "
                 "Use repository tools to read, search, edit, and run focused diagnostics. "
                 "You may modify only these paths: " + json.dumps(task["files"]) + ". "
                 "Do not commit or change branches.\n" + task["prompt"])]
    if record.get("previous_error"):
        from .engine import previous_attempt_note
        sections.append(("previous failure", previous_attempt_note(record["previous_error"])))
    if record.get("journal"):
        sections.append(("journal", "\nJOURNAL\n" + record["journal"]))
    for name in task.get("context_files", []):
        # safe_path was already applied by the supervisor when it validated the
        # plan; re-resolve here so a tampered record cannot read outside.
        from .engine import safe_path
        sections.append(("context " + name,
                         "\nCONTEXT " + name + "\n" + safe_path(workspace, name).read_text()))
    return fit(sections, record.get("context_characters") or DEFAULT_CONTEXT_CHARACTERS)


def run_attempt(record_path):
    record_path = Path(record_path)
    record = json.loads(record_path.read_text())
    workspace = Path(record["workspace"])
    os.environ["AGENT_LOOP_DEPLOY_COMMAND"] = str(Path(__file__).resolve().parent.parent / "scripts" / "merge-deploy")
    lock_path = workspace_lock_path(record["home"], workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    handle = lock_path.open("w")
    try:
        _lock_with_retry(handle)
    except BlockingIOError:
        # Another owner holds this workspace. Touch nothing.
        handle.close()
        return 3
    try:
        legacy_path = legacy_workspace_lock_path(record['home'], workspace)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        if legacy_path == lock_path:
            legacy = handle
        else:
            legacy = legacy_path.open('a')
            try:
                _lock_with_retry(legacy)
            except BaseException:
                legacy.close()
                raise
    except BlockingIOError:
        handle.close()
        return 3
    except BaseException:
        handle.close()
        raise
    try:
        require_unreserved_workspace(record['home'], workspace)
        record.update(pid=os.getpid(), start_identity=process_identity(os.getpid()),
                      state="registered", heartbeat_at=time.time())
        atomic_json(record_path, record)
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    # Continuous attempts record provider ownership at launch;
                    # do not race that write with a stale heartbeat snapshot.
                    if record.get("continuous"):
                        continue
                    current = json.loads(record_path.read_text())
                    current["heartbeat_at"] = time.time()
                    atomic_json(record_path, current)
                except (OSError, ValueError):
                    return

        heart = threading.Thread(target=beat, daemon=True)
        heart.start()
        task = record["task"]
        try:
            continuous = bool(record.get("continuous"))
            agent = (continuing_worker_command(
                task["provider"], record["prompt"], task.get("model"), task.get("effort"),
                record.get("session_id"), workspace,
                allow_subagents=task.get("allow_subagents", True), full_access=task.get("full_access", True),
                allow_git=task.get("allow_git", True))
                if continuous else command(task["provider"], build_prompt(record),
                                           task.get("model"), task.get("effort"),
                                           sandbox=record.get("sandbox", False),
                                           workspace=workspace))
            idle = record.get("worker_idle_timeout_seconds")
            hard = (record.get("worker_max_seconds", 14400) if idle
                    else record.get("worker_timeout_seconds", 180))
            argv = agent if continuous else runner_command(task["provider"], agent, workspace, hard,
                                  record.get("unknown_quota_retry_seconds", 1800),
                                  task.get("quota_bucket", "codex"), idle,
                                  task["files"] if idle else ())
            logs = Path(record["log_directory"])
            logs.mkdir(parents=True, exist_ok=True)
            # Written as output arrives so a running attempt can be followed live.
            stem = record["task_id"] + "-" + record["attempt_id"]
            live = (logs / (stem + ".jsonl"), logs / (stem + ".stderr"))
            def spawned(proc):
                if continuous:
                    record.update(provider_pid=proc.pid,
                                  provider_start_identity=process_identity(proc.pid),
                                  state="running", heartbeat_at=time.time())
                    atomic_json(record_path, record)
            code, out, err = run_process(
                argv, workspace, hard if continuous else record.get(
                    "provider_runner_timeout_seconds", 86400),
                idle_timeout=idle if continuous else None, tee=live,
                activity=(lambda: worktree_activity(workspace)) if continuous else None,
                pass_fds=tuple(dict.fromkeys((handle.fileno(), legacy.fileno()))) if continuous else (), on_spawn=spawned)
            result = parse(task["provider"], code, out, err)
            payload = {"status": result.status, "response": result.response,
                       "usage": result.usage, "retry_at": result.retry_at,
                       "error": result.error, "estimated_usd": result.estimated_usd,
                       "invoked": result.invoked,
                       "session_id": session_id(task["provider"], out) if continuous else None}
        except (OSError, ValueError) as exc:
            payload = {"status": "error", "response": "", "usage": {}, "retry_at": 0,
                       "error": str(exc), "estimated_usd": 0, "invoked": False}
        finally:
            stop.set()
        # The result carries every identity the supervisor will verify, so a
        # result from a superseded attempt can never be reaped as this one's.
        atomic_json(record["result_path"], {
            "attempt_id": record["attempt_id"], "nonce": record["nonce"],
            "run_id": record["run_id"], "task_id": record["task_id"],
            "base": record["base"], "policy_version": record["policy_version"],
            "finished_at": time.time(), "result": payload})
        record["state"] = "result_written"
        record["heartbeat_at"] = time.time()
        atomic_json(record_path, record)
        return 0
    finally:
        # A continuous provider inherits this descriptor. Closing the wrapper's
        # copy keeps the lock held if a descendant outlives the wrapper.
        if not record.get("continuous"):
            fcntl.flock(handle, fcntl.LOCK_UN)
            if legacy is not handle:
                fcntl.flock(legacy, fcntl.LOCK_UN)
        if legacy is not handle:
            legacy.close()
        handle.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m loop.worker <attempt-record.json>", file=sys.stderr)
        return 2
    return run_attempt(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
