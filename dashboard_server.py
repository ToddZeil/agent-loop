#!/usr/bin/env python3
"""Serve the local agent-loop dashboard and expose read-only run state."""

from __future__ import annotations

import json
import argparse
import re
import subprocess
import sqlite3
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DASHBOARD = ROOT / "dashboard"
DB_PATH = ROOT / ".agent-loop" / "state.sqlite"


def _rows(connection, query, args=()):
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query, args).fetchall()]


def read_process_agents():
    """Find live loop supervisors/workers without exposing their full commands."""
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=,etime=,command="],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    agents = []
    for line in result.stdout.splitlines():
        if "dashboard_server.py" in line or "ps ax" in line or "rg '" in line:
            continue
        command_lower = line.lower()
        if not any(marker in command_lower for marker in ("python -m loop", "loop.worker", "run_provider.py")):
            continue
        pid_match = re.match(r"\s*(\d+)\s+(\S+)\s+(.*)", line)
        if not pid_match:
            continue
        pid, elapsed, command = pid_match.groups()
        repo_match = re.search(r"--repo\s+(\S+)", command)
        if repo_match:
            repo = repo_match.group(1)
        else:
            attempt_match = re.search(r"(\S+)/\.agent-loop/attempts/[^\s]+\.json", command)
            repo = attempt_match.group(1) if attempt_match else ""
        repo = repo.split("/.agent-loop", 1)[0].rstrip("/")
        if not repo:
            continue
        if "loop.worker" in command:
            role = "worker"
        elif " lite " in command:
            role = "lite supervisor"
        elif " run " in command:
            role = "supervisor"
        else:
            role = "provider runner"
        provider_match = re.search(r"--provider\s+(\S+)", command)
        lite_match = re.search(r"\blite\s+(?:resume|start)\s+(\S+)", command)
        plan_match = re.search(r"\brun\s+[^\s]+/([^/]+)\.json", command)
        agents.append({
            "pid": int(pid),
            "elapsed": elapsed,
            "project": Path(repo).name,
            "repo": repo,
            "role": role,
            "provider": provider_match.group(1) if provider_match else "agent-loop",
            "run_id": lite_match.group(1) if lite_match else (plan_match.group(1) if plan_match else ""),
            "command_kind": "worker" if "loop.worker" in command else "supervisor",
            "state": "working",
        })
    return agents


def read_repo_runs(project_root):
    """Read unfinished run progress for one repository."""
    db_path = project_root / ".agent-loop" / "state.sqlite"
    if not db_path.exists():
        return []
    try:
        connection = sqlite3.connect(db_path, timeout=1)
        connection.row_factory = sqlite3.Row
        runs = [dict(row) for row in connection.execute("SELECT * FROM runs ORDER BY id").fetchall()]
        tasks = [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY run_id, id").fetchall()]
        connection.close()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for run in runs:
        run_tasks = [task for task in tasks if task["run_id"] == run["id"]]
        done = sum(task["status"] == "done" for task in run_tasks)
        if not run_tasks or done == len(run_tasks):
            continue
        current = next((task for task in run_tasks if task["status"] == "running"), None)
        current = current or next((task for task in run_tasks if task["status"] in {"waiting", "pending"}), None)
        result.append({
            "id": run["id"],
            "branch": run["branch"],
            "workspace": run["workspace"],
            "repo": str(project_root),
            "project": project_root.name,
            "done": done,
            "total": len(run_tasks),
            # Cross-repository views intentionally expose only progress metadata.
            "tasks": [{"id": task["id"], "status": task["status"], "attempts": task["attempts"]} for task in run_tasks],
            "current_task": current["id"] if current else "",
            "current_status": current["status"] if current else "",
        })
    return result


def read_active_workers(project_roots, live_runs):
    """Return one row per currently leased worker task, not supervisors/wrappers."""
    workers = []
    for project_root in project_roots:
        attempts_dir = project_root / ".agent-loop" / "attempts"
        if not attempts_dir.exists():
            continue
        for path in attempts_dir.glob("*.json"):
            try:
                attempt = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            heartbeat = float(attempt.get("heartbeat_at") or attempt.get("created_at") or 0)
            if attempt.get("state") not in {"intended", "registered", "running"} or not heartbeat or time.time() - heartbeat >= 6 * 3600:
                continue
            task = attempt.get("task") or {}
            repo = str(project_root)
            run_id = attempt.get("run_id", "")
            run = next((item for item in live_runs if item["repo"] == repo and item["id"] == run_id), None)
            workers.append({
                "pid": attempt.get("pid"),
                "project": project_root.name,
                "repo": repo,
                "run_id": run_id,
                "task_id": attempt.get("task_id") or task.get("id") or "worker",
                "attempt": attempt.get("attempt", 1),
                "provider": task.get("provider") or "agent",
                "model": task.get("model"),
                "state": "working",
                "heartbeat_at": heartbeat,
                "progress": {"done": run["done"], "total": run["total"], "task": run["current_task"], "status": run["current_status"]} if run else {"done": 0, "total": 0, "task": "", "status": "working"},
            })
    return workers


def read_provider_usage(project_root):
    """Return numeric provider usage plus locally cached quota percentages."""
    usage = {name: {"tokens": 0, "calls": 0, "input": 0, "output": 0, "cached": 0, "usd": 0.0, "quota": {}}
             for name in ("codex", "claude", "antigravity")}
    db_path = project_root / ".agent-loop" / "state.sqlite"
    if db_path.exists():
        try:
            connection = sqlite3.connect(db_path, timeout=1)
            for row in connection.execute("SELECT name,tokens,estimated_usd FROM providers"):
                if row[0] in usage:
                    usage[row[0]]["tokens"] = row[1] or 0
                    usage[row[0]]["usd"] = row[2] or 0.0
            for row in connection.execute("SELECT provider,COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(cached_tokens),0),COALESCE(SUM(estimated_usd),0) FROM invocations GROUP BY provider"):
                if row[0] in usage:
                    usage[row[0]].update({"calls": row[1], "input": row[2], "output": row[3], "cached": row[4], "usd": row[5] or usage[row[0]]["usd"]})
            connection.close()
        except sqlite3.Error:
            pass

    def load_json(name):
        try:
            return json.loads((project_root / ".agent-loop" / name).read_text())
        except (OSError, ValueError):
            return {}

    codex = load_json("codex-quota.json").get("rateLimits", {}).get("codex", load_json("codex-quota.json").get("rateLimits", {}))
    if codex:
        usage["codex"]["quota"] = {
            "short_term": codex.get("primary", {}).get("usedPercent"),
            "weekly": codex.get("secondary", {}).get("usedPercent"),
            "source": "quota snapshot",
        }
    claude = load_json("claude-quota.json").get("rate_limits", {})
    if claude:
        usage["claude"]["quota"] = {
            "short_term": claude.get("five_hour", {}).get("used_percentage"),
            "weekly": claude.get("seven_day", {}).get("used_percentage"),
            "source": "quota snapshot",
        }
    return usage


def read_state(db_path=DB_PATH, project_root=ROOT):
    if not db_path.exists():
        return {"generated_at": time.time(), "runs": [], "providers": [], "events": [], "available": False}

    connection = sqlite3.connect(db_path, timeout=2)
    try:
        runs = _rows(connection, "SELECT * FROM runs ORDER BY id")
        tasks = _rows(connection, "SELECT * FROM tasks ORDER BY run_id, id")
        providers = _rows(connection, "SELECT * FROM providers ORDER BY name")
        events = _rows(
            connection,
            "SELECT at, run_id, task_id, kind, detail FROM events ORDER BY at DESC LIMIT 18",
        )
    finally:
        connection.close()

    task_map = {}
    for task in tasks:
        task_map.setdefault(task["run_id"], []).append(task)

    result_runs = []
    for run in runs:
        run_tasks = task_map.get(run["id"], [])
        try:
            plan_tasks = {item.get("id"): item for item in json.loads(run.get("plan") or "{}").get("tasks", [])}
        except (TypeError, ValueError):
            plan_tasks = {}
        for task in run_tasks:
            definition = plan_tasks.get(task["id"], {})
            task["prompt"] = definition.get("prompt", "")
            task["files"] = definition.get("files", [])
            task["check"] = definition.get("check", [])
            task["depends_on"] = definition.get("depends_on", [])
        done = sum(task["status"] == "done" for task in run_tasks)
        run_copy = {key: run[key] for key in ("id", "branch", "workspace", "reason", "parent_id")}
        run_copy.update({"tasks": run_tasks, "done": done, "total": len(run_tasks)})
        result_runs.append(run_copy)

    # Task rows are updated around worker launches. During that small window,
    # the durable attempt lease is the freshest indication that an agent is
    # actually alive. Ignore old result-written or expired attempt records.
    active_attempts = []
    attempts_dir = project_root / ".agent-loop" / "attempts"
    now = time.time()
    if attempts_dir.exists():
        for path in attempts_dir.glob("*.json"):
            try:
                attempt = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            heartbeat = float(attempt.get("heartbeat_at") or attempt.get("created_at") or 0)
            state = attempt.get("state")
            # A worker lease can be long-lived, but a several-day-old record
            # is not evidence of a current agent.
            if state in {"intended", "registered", "running"} and heartbeat and now - heartbeat < 6 * 3600:
                task = attempt.get("task") or {}
                active_attempts.append({
                    "run_id": attempt.get("run_id"),
                    "task_id": attempt.get("task_id") or task.get("id"),
                    "attempt": attempt.get("attempt", 1),
                    "provider": task.get("provider") or "agent",
                    "model": task.get("model"),
                    "state": state,
                    "heartbeat_at": heartbeat,
                })

    process_agents = read_process_agents()
    provider_usage = read_provider_usage(project_root)
    live_runs = []
    seen_repos = {Path(agent["repo"]) for agent in process_agents}
    seen_repos.add(project_root)
    for repo in seen_repos:
        live_runs.extend(read_repo_runs(repo))
    active_workers = read_active_workers(seen_repos, live_runs)
    for agent in process_agents:
        matching = [run for run in live_runs if run["repo"] == agent["repo"] and (not agent["run_id"] or run["id"] == agent["run_id"])]
        run = matching[0] if matching else next((run for run in live_runs if run["repo"] == agent["repo"]), None)
        if run:
            current_task = run["current_task"]
            agent["run_id"] = run["id"]
            agent["progress"] = {"done": run["done"], "total": run["total"], "task": current_task, "status": run["current_status"]}
        else:
            agent["progress"] = {"done": 0, "total": 0, "task": "", "status": "working"}

    return {
        "generated_at": time.time(),
        "available": True,
        "runs": result_runs,
        "providers": providers,
        "events": events,
        "active_attempts": active_attempts,
        "process_agents": process_agents,
        "worker_agents": active_workers,
        "live_runs": live_runs,
        "provider_usage": provider_usage,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    db_path = DB_PATH
    project_root = ROOT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    def do_GET(self):  # noqa: N802 - required by the stdlib server API
        if urlparse(self.path).path == "/api/state":
            payload = json.dumps(read_state(self.db_path, self.project_root), default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, format, *args):  # noqa: A002 - matches stdlib signature
        return


def main():
    parser = argparse.ArgumentParser(description="Serve the local agent-loop dashboard")
    parser.add_argument("--repo", default=str(ROOT), help="Repository whose .agent-loop state should be shown")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    project_root = Path(args.repo).expanduser().resolve()
    DashboardHandler.project_root = project_root
    DashboardHandler.db_path = project_root / ".agent-loop" / "state.sqlite"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Agent Loop dashboard: http://127.0.0.1:{args.port} (repo: {project_root})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
