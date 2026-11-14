# Working in this repository

## Application entry points

The current local supervisor workspace runs with `scripts/supervise-web` at
`http://127.0.0.1:8765`. Its browser files are in `web/`; the server is `loop/web.py`,
worker scheduling is `loop/supervise.py`, provider commands are in `loop/adapters.py`,
and detached execution is in `loop/worker.py`.

`dashboard/` and `dashboard_server.py` are an older, separate dashboard. Changes there
do not appear in the supervisor workspace. When working on Add agents, model/effort
selection, worker conversations, or local terminals, start with `web/` and `loop/web.py`.

The Conversation view forwards structured provider output. The Agent terminal tab
opens the real interactive CLI over a PTY, yielding only that agent at a turn boundary.
Its chat ID and worktree are persisted for recovery. The separate worktree shell is
also a PTY. Messages in the web composer are queued for the next supervised turn;
keystrokes in the Agent terminal go directly to the interactive CLI.
Managed workers and native terminals default to full access without tool approval
prompts. The per-agent `full_access` setting is captured in each worker attempt;
permission changes apply next turn or after reopening the interactive terminal.
Custom CLI commands supply their own permission flags.

## Development and validation

- Install dependencies with `scripts/setup-supervise-web` on a fresh checkout.
- Start an independent development instance with `scripts/supervise-web --port 8777`.
  The shared instance on 8765 may have active workers; inspect it before restarting it.
- Browser assets are served directly; there is no bundler build step.
- Run Python tests with `.agent-loop/web-venv/bin/python -m unittest discover -s tests`.
- Run browser tests with `npm --prefix web test` after installing Playwright Chromium
  (`cd web && npx playwright install chromium`). The browser fixtures use fake providers.
- Worker worktrees start from committed `HEAD`, not from uncommitted changes in another
  checkout. Completed changes remain on the worker branch until integrated into the
  checkout serving the application. Restarting a server does not integrate branches.
- During a supervised run, follow the runtime instructions about Git ownership; the
  manager normally verifies and commits the worker's result.

## Publishing worker changes to the running workspace

When the user requests a merge/deployment, run `"$AGENT_LOOP_DEPLOY_COMMAND"` from
this repository's worker worktree. The canonical command is
`/Users/thall/AndroidStudioProjects/didnteventry/agent-loop/scripts/merge-deploy`.
The browser offers **Agent settings → Merge & redeploy** for registered workers in
this repository. This managed operation is allowed even though manual worker Git
commits/branch changes remain keeper-owned: it snapshots edits with a private index,
validates an isolated merge, pushes main, fast-forwards the serving checkout and
reopens active chats in the existing supervisor home. It never changes the worker's
branch or staged files. Do not start a separate preview as a substitute for updating
port 8765. Read the returned status file and sibling `.log` for progress/errors.
