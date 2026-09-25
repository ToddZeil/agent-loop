#!/usr/bin/env python3
"""Keep `muse exec` working on a goal all night, resuming when its usage limit resets.

    python3 scripts/muse_keeper.py "Fix the failing tests" --workspace /path/to/repo \
        --check "pytest -q" --sleep 1800

Each round runs one headless muse turn on a stable --session-id. A local Ollama model
reads the tail of the output and says LIMIT (usage hit: sleep, retry), DONE, or CONTINUE.
DONE is only believed when --check passes. Every round's raw output is kept in
<workspace>/.muse-keeper/ so token usage can be totalled afterwards.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

LIMIT_RE = re.compile(r"usage limit|quota|rate.?limit|too many requests|\b429\b|"
                      r"insufficient (credit|balance)|limit (reached|exceeded)|try again (in|at)",
                      re.I)
PROMPT = ("Classify the end of an autonomous coding agent's run. Reply JSON only: "
          '{"state": "LIMIT"|"DONE"|"CONTINUE"}. LIMIT = it stopped because of a usage, '
          "quota, credit or rate limit. DONE = it says the goal is finished. CONTINUE = "
          "anything else.\n\nOutput tail:\n")


def classify(tail, model, host, code):
    # A real limit stops the run with a nonzero exit. Exit 0 is a finished turn, and its
    # text can mention "rate limit" as page content (these SEO pages document them).
    if code != 0 and LIMIT_RE.search(tail):
        return "LIMIT"
    try:
        body = json.dumps({"model": model, "prompt": PROMPT + tail, "format": "json",
                           "stream": False, "options": {"temperature": 0}}).encode()
        req = urllib.request.Request(host + "/api/generate", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            state = json.loads(json.loads(resp.read())["response"]).get("state")
        if state == "LIMIT" and code == 0:
            state = "CONTINUE"
        return state if state in ("LIMIT", "DONE", "CONTINUE") else "CONTINUE"
    except (OSError, ValueError, KeyError):
        return "CONTINUE"  # Ollama down: keep going rather than stall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--check", help="Shell command that must pass before DONE is accepted")
    ap.add_argument("--sleep", type=int, default=1800, help="Seconds to wait after a limit")
    ap.add_argument("--max-rounds", type=int, default=200)
    ap.add_argument("--round-timeout", type=int, default=3600)
    ap.add_argument("--muse-arg", action="append", default=[],
                    help="Extra muse exec argument, repeatable (e.g. --muse-arg=--model=X)")
    ap.add_argument("--until-commits", type=int, default=0,
                    help="Stop only after this many new commits (e.g. one per batch)")
    ap.add_argument("--stagnant-limit", type=int, default=3,
                    help="Stop after this many rounds that change neither HEAD nor the tree")
    ap.add_argument("--keeper-model", default="qwen2.5-coder:7b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    args = ap.parse_args()

    work = Path(args.workspace).resolve()
    logs = work / ".muse-keeper"
    logs.mkdir(exist_ok=True)
    session = str(uuid.uuid4())
    note = ""
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
                          text=True).stdout.strip()

    def commits():
        r = subprocess.run(["git", "rev-list", "--count", base + "..HEAD"], cwd=work,
                           capture_output=True, text=True)
        return int(r.stdout.strip() or 0)

    def fingerprint():
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
                              text=True).stdout
        diff = subprocess.run(["git", "diff", "HEAD"], cwd=work, capture_output=True,
                              text=True).stdout
        return head + hashlib.sha256(diff.encode()).hexdigest()

    last, stagnant = fingerprint(), 0
    for n in range(1, args.max_rounds + 1):
        # The goal rides along every round, so a session that muse does not resume
        # still gets the whole task.
        prompt = "Goal: %s\n\n%s" % (args.goal, note or "Begin.") if n == 1 else (
            "Goal: %s\n\nContinue where you left off. %s" % (args.goal, note))
        argv = ["muse", "exec", "--json", "--workspace", str(work), "--session-id", session,
                "--approval-mode", "never", *args.muse_arg, prompt]
        log = logs / ("round-%03d.log" % n)
        # Stream to the log as it happens so `tail -f` shows what muse is doing.
        with open(log, "w") as fh:
            proc = subprocess.Popen(argv, cwd=work, stdout=fh, stderr=subprocess.STDOUT, text=True)
            try:
                code = proc.wait(timeout=args.round_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                fh.write("\n[timeout]")
                code = 124
        out = log.read_text(errors="replace")
        state = classify(out[-3000:], args.keeper_model, args.ollama, code)
        print("[%s] round %d exit=%d -> %s" % (time.strftime("%H:%M:%S"), n, code, state),
              flush=True)
        now = fingerprint()
        stagnant = 0 if now != last or state == "LIMIT" else stagnant + 1
        last = now
        if stagnant >= args.stagnant_limit:
            print("no repo change for %d rounds; stopping (see the last round log)" % stagnant,
                  flush=True)
            return 2
        if state == "LIMIT":
            time.sleep(args.sleep)
            note = "(You were paused by a usage limit.)"
            continue
        if args.until_commits and commits() < args.until_commits:
            print("  %d/%d commits; continuing" % (commits(), args.until_commits), flush=True)
            if state != "LIMIT":
                note = ("Next: %d of %d batches are committed. Start the next batch now."
                        % (commits(), args.until_commits))
                time.sleep(5)
            continue
        if state == "DONE" or code == 0 and args.check:
            if not args.check:
                return 0
            chk = subprocess.run(args.check, shell=True, cwd=work, capture_output=True, text=True)
            if chk.returncode == 0:
                print("check passed; done")
                return 0
            note = "The check `%s` still fails:\n%s" % (args.check, (chk.stdout + chk.stderr)[-2000:])
        else:
            note = ""
            time.sleep(5)
    print("max rounds reached")
    return 1


if __name__ == "__main__":
    sys.exit(main())
