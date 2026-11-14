# Working in this repository

## Application entry points

The current local supervisor workspace runs with `scripts/supervise-web` at
`http://127.0.0.1:8765`. Its browser files are in `web/`; the server is `loop/web.py`,
worker scheduling is `loop/supervise.py`, provider commands are in `loop/adapters.py`,
and detached execution is in `loop/worker.py`.

`dashboard/` and `dashboard_server.py` are an older, separate dashboard. Changes there
do not appear in the supervisor workspace. When working on Add agents, model/effort
selection, worker conversations, or local terminals, start with `web/` and `loop/web.py`.

The readable worker conversation forwards structured provider output. Writable shells
are separate PTY sessions. Messages to supervised workers are queued for their next turn;
they do not type into an active provider process.

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
