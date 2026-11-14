# One local supervisor, many agents

The browser app runs one persistent supervisor. Add or remove agents whenever needed;
there is no need to create separate fleets. Each agent has its own repository, branch,
worktree, provider/model, conversation, progress, and optional subagent setting. The
stronger supervisor answers questions across agents using one serialized, resumable
conversation. Deterministic scheduling continues while the model is idle.

## Launch

Requires Python 3.11+, Node/npm for browser assets, and macOS or Linux for local terminals.

```sh
scripts/setup-supervise-web
scripts/supervise-web
```

Open **http://127.0.0.1:8765**. Dependencies are installed under `.agent-loop/web-venv` and
`web/node_modules`; browser assets are served locally. Both scripts work by absolute path
from another directory. Re-run setup after dependency changes.

```sh
scripts/supervise-web --port 9000 --home /absolute/path/to/browser-registry
```

Keep the registry directory at the same location to retain project selections, agent
state, and the shared supervisor conversation. One server owns each registry.

## Project library

Click **Projects** and scan a directory containing your repositories, or add paths directly
(one per line). Repositories are saved once and deduplicated by their resolved root path.
Existing registered agents also populate the library. The **Saved project** dropdown in
agent setup fills the repository path automatically; manual paths remain supported.

Scans have a configurable depth (default 4), skip symlinks, dependency directories and
`.agent-loop`, stop descending once a repository is found, and cap traversal at 10,000
directories. Linked Git worktrees with a `.git` file are recognized. **Rescan saved folders**
finds projects added later. Removing a saved entry does not delete its repository or
remove an agent; rescanning its containing folder can discover it again.

## Add, remove, and restore agents

Use **Add agents** to choose a saved repository, enter a goal, select a worker provider/model,
and choose whether subagents are allowed. Agent IDs are generated automatically. You can
add several agents in one dialog.

**Optional settings** contains editable agent IDs, custom verification commands, fallback
providers, and the raw JSON editor (which accepts a manifest's `tasks` array). No verification
command is required: the shared supervisor reviews each result against its goal by default,
and sends failed reviews back to the worker with repair feedback. Busy or usage-limited
reviews wait without rerunning the worker. Custom commands, when supplied, must pass;
commands already passing at baseline also require objective review.

**Add agents** joins them to the existing supervisor. If scheduling is already running,
they start as capacity becomes available. **Add & start supervisor** also starts a stopped
supervisor. With no work left, the supervisor stays ready for new agents. Its parallelism
limit can be changed in settings without replacing the supervisor.

**Remove agent** stops scheduling further turns for that agent. Its current owned turn
finishes and is reconciled; its branch, edits, session, logs, and results remain available.
The card shows `stopping` until that turn finishes, then moves to **Removed agents**.
Click the removed agent to restore it. A completed agent stays completed when restored;
use a new agent ID for a new objective.

Older browser fleets are adopted into the single supervisor while preserving their existing
worktrees and progress. Old managers are asked to stop scheduling so the new manager can
reconcile their detached workers. **Import saved agents** can also adopt CLI runs; stop
the source manager first. Duplicate display IDs receive a suffix; actual worktree identities
are preserved. The batch `supervise` CLI remains compatible with saved runs.

## Model choices and corrections

Worker, fallback, and supervisor models have dropdowns with exact provider IDs. Codex
choices come from its local `models_cache.json` catalogue; hidden entries are excluded.
The web app saves this list in its own `models.json` and retains it if the CLI cache is
unavailable. Reopening setup/settings reloads the local catalogue without a model call.
Other providers initially offer their default and saved custom IDs; automatic discovery
for those CLIs is not implemented. Choose **Custom model ID…** to save another ID.
A saved custom ID is not a guarantee of account access.

Unsupported-model errors block for correction rather than retrying on a quota timer.
To correct an existing agent, stop scheduling, let its current turn finish, choose its
**Agent model**, and click **Save model**. Start the supervisor again to retry. This clears
the old worker session and preserves the worktree, goal, and usage history.

## Effort settings

**Worker effort** is available in Add agents and the selected agent's detail panel.
**Save effort** applies changes to the next worker turn without discarding its worktree
or session. **Supervisor effort** is in Supervisor settings and applies to both questions
and objective reviews. The model catalogue supplies Codex's advertised effort levels;
providers without metadata offer low, medium, and high. The selected provider/model must
support the setting. Antigravity models with an effort suffix use that embedded effort.

## Worker permissions

Supervised agents can edit every project file in their own worktree. File allowlists and
`protected` paths are not applied in this mode, including legacy settings. Git history,
worktree ownership, and supervisor control metadata remain managed by the loop. The original
checkout is preserved, and scope controls in the separate full/continuous workflows are
unchanged.

**Allow subagents** is configurable when adding an agent and in its detail panel. It is off
by default for compatibility. Changes apply to the next worker turn; running turns retain
what they started with. Codex native multi-agent flags and Claude's Agent-tool restriction
follow this setting; other providers receive matching delegation instructions, with actual
capabilities determined by their CLI. Subagents coordinate within the parent agent's
worktree. The supervisor itself stays read-only and does not delegate.

The manager verifies results and commits completed changes. A reviewed result needing no
edits can finish at the existing commit when no custom commands are configured. Workers are instructed
not to commit or change branches. Publishing and merging remain separate actions. Provider
permissions are unchanged: Codex uses a workspace sandbox; other CLI permission behavior
is documented in the adapters. This is a trusted local coding environment, not uniform
host-wide isolation across providers.

## Optional local keeper

Open **Supervisor settings** to choose the stronger supervisor provider/model, parallel
agents, and optional local keeper. The keeper is **off by default**.

When enabled, an already installed Ollama model at `127.0.0.1:11434` summarizes progress
and produces recovery context for the stronger model. The app does not install models or
start Ollama implicitly. If the local service/model is unavailable, summaries fall back to
rules and saved evidence. Turning it off makes no local-model calls and does not disable
recovery, quota handling, or the stronger supervisor.

The shared supervisor session is persisted and resumed for questions from different
agents. Context limits or recoverable session failures start a fresh session using saved
handoff, goals, checks, and decisions. Quota holds are respected. A deterministic browser
service watchdog can restart a crashed manager from durable state while scheduling remains
requested. A local model's summary never overrides acceptance or grants new permissions.

## Live streams and completion signals

The app receives server-sent events at `/api/events`. A changed supervisor/agent snapshot
is sent as a `state` event; `tick` events refresh the selected agent's bounded activity
stream. EventSource reconnects automatically and receives the latest full snapshot.

Signals come from the provider's structured CLI output and durable worker results:

- `working`: a worker owns an active attempt; silence does not mean completion.
- `supervisor`: the stronger supervisor is answering this agent.
- `blocked`: a question, authentication problem, or operator decision needs attention.
- `quota`: exhausted provider allowance is waiting for its recorded reset.
- `retrying`: a recoverable error has a scheduled retry.
- `verifying`: the returned result is undergoing checks or supervisor review.
- `complete`: objective verification passed; any delivered changes were committed.
- `stopping` / `removed`: the agent has been removed from scheduling.

A CLI `turn.completed` event means a turn finished, not that the objective passed. Worker
output is written as it arrives, while questions are detected from the returned turn text.
An idle timeout is recovery evidence, never a successful-completion signal. The system does
not scrape terminal pixels to decide whether an agent is done.

## Worker messages

Sending direction saves it for the next worker turn and automatically starts a stopped
supervisor. A follow-up to a completed worker reopens it using its existing worktree and
session. Messages sent during an active turn remain queued until that turn finishes.
If a writable shell or another owner prevents startup, the message stays saved and the
UI explains why startup failed. Close that shell and start the supervisor to continue.
Messages do not inject keystrokes into an active provider process.

## Local terminals and controls

Select an agent and click **View terminal**, or open its **Terminal** tab. **Conversation**
shows messages and expandable commands with their output while the agent runs, and retains the six most recent
turns after completion. Sent messages appear with queued/delivered status. Direct links support `/?agent=task-3&tab=terminal`. On smaller
screens, selecting an agent scrolls its detail panel into view.

The separate **Open terminal** button opens a writable worktree shell after scheduling
has stopped and the current worker turn has finished. Viewing worker output requires
neither stopping the manager nor opening that shell.

The Activity tab shows worker output; Decisions shows supervisor answers. Operator notes
are durable and delivered before the next turn. Stop scheduling leaves in-flight workers
running; starting the supervisor reconciles their results before continuing.

The Terminal tab is a real interactive local shell in the selected agent's worktree,
with keyboard input, resizing, and reconnection to the same process while the web server
lives. It is separate from the structured worker CLI session. Stop scheduling and reconcile
outstanding attempts before opening a writable shell. Ownership locks prevent simultaneous
manual-shell and worker writers. Close the shell before restarting scheduling.

Browser disconnection does not kill a shell. **Close shell** releases its ownership; stopping
the web server closes its shells. Detached managers and worker turns survive web-server
shutdown. Use Ctrl-C in the server terminal to stop the web server.

The server binds only to loopback, validates Host/Origin, uses a same-site HTTP-only cookie,
and requires an explicit mutation header. It serves named UI assets, not repository files.
It is intended for local use rather than a public reverse proxy.

## Tests

```sh
.agent-loop/web-venv/bin/python -m unittest tests.test_web tests.test_supervise tests.test_loop.AdapterTests
web/node_modules/.bin/playwright install chromium
npm --prefix web test
```

Tests use disposable repositories, real local shells, and simulated worker CLIs. Browser
artifacts are saved under `.agent-loop/`. Set `PLAYWRIGHT_BROWSERS_PATH` consistently if
using a custom test-browser cache.

## Token monitoring

The **Token monitoring** section shows cumulative reported usage for all agents and the
shared supervisor, with input, output, and cached-input counts. Expand **Supervisor usage
by model** for its provider/model breakdown. Each agent's **Tokens** tab shows its own
breakdown, model history, last reported context estimate, and worker budget when configured.

Agent totals include worker and any legacy verification-review usage. Shared supervisor
reviews and assistance appear separately on that agent's Tokens tab and count once in the
shared supervisor total.
Removed agents retain their consumption in lifetime totals. Session/context resets,
provider fallback, model changes, and browser/server restarts do not reset recorded totals.

Counts come from the CLI's usage reports, usually at the end of a turn, and update through
the live stream when the manager records them. A CLI that does not report usage displays
unavailable fields as `—`; no token estimates are invented from output length. Token counts
cover tracked runs, not other terminal sessions or the provider's entire account. The
optional local keeper's Ollama usage is not included in these agent/supervisor totals.

Cached tokens are a subset of input and are not added twice. Codex input already includes
cached input; Claude's separate cache-read and cache-creation counters are included in its
input total. Cache-creation counts are also available separately through the API. Older
agent totals are retained, but missing historical input/output breakdowns and supervisor
usage cannot be reconstructed. The UI identifies incomplete reporting.

Usage is saved in each run's `continuous.json` under `token_usage`, with stable attempt/call
IDs preventing duplicate accounting. The API exposes summaries rather than internal IDs.
Supervisor totals aggregate attributed supervisor calls across agents, so changing the
shared supervisor conversation or model preserves its totals. Snapshot revisions prevent
a delayed HTTP response from rolling back newer streamed counts.

## Shipping changes and future workers

Workers start from committed source `HEAD`, never from another checkout's uncommitted
files. Their changes stay on their own worktree branches; restarting a server does not
apply them to the checkout serving the app. Integrate and test the desired changes in
the serving checkout, commit, and push that branch before starting workers that need
those features. Existing worker worktrees keep their own history.

## Identifying the interface in an open tab

The sidebar shows an **UI** revision derived from the served HTML, CSS, and JavaScript.
Asset URLs include that revision so a refreshed page fetches the matching files. When
those files change, existing tabs receive an **Updated interface available** notice.
**Reload interface** preserves the selected worker and any unsent worker-message draft.
The app does not reload an active tab automatically. Worktree previews on another port
still serve their own checkout; a revision notice does not merge worktree changes.
