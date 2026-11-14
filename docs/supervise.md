# Supervise independent coding tasks

`supervise` keeps independent coding workers moving across any explicitly named local Git
repositories. Each task gets its own branch and worktree, persistent provider conversation,
logs, optional verification commands, and recovery records. The supervisor reviews results
when no custom verification proves the objective, and answers worker questions or helps
with timeouts and repeated check failures. No Ollama service is required.

Copy [the example manifest](../examples/supervise.json) and replace the repository paths,
goals, and checks. Relative `repo` paths resolve against the manifest's directory. Multiple
tasks may use the same repository; they still receive independent worktrees. Worktrees
start from committed `HEAD` (or the specified `base`), so commit any changes workers need
first. Existing working files in the source checkout are preserved.

Run these commands from the agent-loop checkout; the target repositories can be elsewhere:

```sh
python3 -m loop supervise start /path/to/tasks.json --home /path/to/new-fleet --detach
python3 -m loop supervise status /path/to/new-fleet
python3 -m loop supervise note /path/to/new-fleet api-fix "Use the existing cursor format."
python3 -m loop supervise stop /path/to/new-fleet
python3 -m loop supervise resume /path/to/new-fleet --detach
```

To invoke it from any directory, use the executable wrapper (or put `scripts` on `PATH`):

```sh
/path/to/agent-loop/scripts/supervise start ./tasks.json --home /path/to/new-fleet --detach
```

Omit `--detach` to run in the foreground. `resume --once` performs one scheduling pass.
The fleet directory must be new on `start`; reuse it with `resume`. Put it outside tracked
source files, for example under `.agent-loop/fleets/`. The global `--repo` option is not
used by this command: each manifest task names its own repository explicitly.

`status` emits JSON with each task's worktree, branch, session, state, checks, decisions,
turns, and accounting. `manager.log` contains detached manager output. Task logs and
control records live under `<repo>/.agent-loop/lite/<run-id>/`; the worktree is printed
in status. Worktree IDs include a hash of the fleet's absolute path, so separate fleets
can reuse task names. Keep the fleet directory at the same location when resuming.

`max_parallel` bounds simultaneous task operations and workers; set it to JSON `null`
to remove the worker scheduling cap. Slow checks or supervisor
calls run independently. One lock protects the fleet manager and another protects each
run; a second manager or lite keeper cannot take over an owned run. Workers hold their
own worktree ownership locks, and a restarted manager reconciles durable results before
launching another turn. Stop requests cease scheduling; in-flight checks or supervisor
calls finish, and detached workers continue. Resume reconciles those workers. Ctrl-C also
leaves worker progress available. Stop requests are cleared when a new manager starts.

The supervisor answers routine questions from the goal, repository, check evidence, and
prior decisions. Question detection uses the existing transcript heuristic, including a
question mark or phrases such as “please confirm” at the end of a turn. It is not an
attachment to an existing terminal or an in-turn interactive approval handler. Workers
use unattended CLI sessions; tmux can be used to watch logs independently.

The supervisor returns either a specific continuation instruction or an operator escalation.
Malformed replies and unavailable supervisors leave only that task waiting. Supervisor quota
limits schedule a timed retry and keep the pending question. `note` supplies
operator direction, which takes priority over a pending automatic intervention. Resume the
fleet if it has exited. The manager exits when every task is complete or awaiting operator
input without a timed retry; its exit code is 0 for all complete, 3 otherwise.

Each task may omit `acceptance` or use an empty list. By default the configured supervisor
reviews the goal, worker result, diff, and relevant repository files in read-only mode. A
failed review sends feedback back to the worker for repair. Reviews use the same serialized
supervisor conversation and token accounting as question handling. A busy supervisor or
usage limit queues the review without launching another worker turn.

Optional `acceptance` shell commands execute inside the worktree and must pass. If they
already passed at baseline, the supervisor must also review the objective. Use substantive
checks: a passing command alone does not establish useful completion. Commands are trusted
local code, so review manifests before running them. The verifier checks for modifications
during verification and commits completed changes. With no custom commands, a reviewed
result requiring no edits can finish at the existing commit.

Options under each task use continuous lite's configuration, including `setup`, `allow_subagents`, `workers`, `worker_model`, `worker_models`, `worker_effort`,
`worker_idle_seconds`, `worker_max_seconds`,
`check_timeout_seconds`, `reset_at_tokens`, and `max_worker_tokens`. Token limits are
admission limits checked between turns, not hard billing caps. Supervisor settings accept
`provider` (codex, claude, antigravity, muse), optional `model` and `effort`, `timeout_seconds`,
`max_interventions`, and `max_turns`. Intervention and turn limits are per task and survive
restarts. The default worker is Codex. Configure an ordered `options.workers` list to allow provider
fallback. `worker_model` sets the primary model; `worker_models` maps each provider to its
model ID and takes precedence. IDs pass through to the configured CLI; there is no fixed
model catalog in this mode. The provider must support the requested model and be logged in.
Arbitrary CLI binaries and unsupported providers still need an adapter.

Automatic decisions may not authorize destructive actions, publishing, credentials, or
spending beyond the objective. Such decisions require operator input. Commits stay on
task branches; pushing, merging, and resolving conflicts between independent tasks remain
operator actions. This command does not continuously combine branches.

## Usage windows and session resets

Example task options:

```json
{
  "workers": ["codex", "claude"],
  "worker_models": {"codex": "YOUR_CODEX_MODEL", "claude": "YOUR_CLAUDE_MODEL"},
  "worker_effort": "medium",
  "return_to_primary": true,
  "unknown_retry_seconds": 1800,
  "reset_at_tokens": 120000
}
```

On a usage-limit result the run records a durable hold for that provider, combining the
reported retry time with exhausted windows from the provider's existing quota adapter.
Both a short window (such as five hours) and a longer window must have allowance; the latest
exhausted reset wins. A usable configured fallback continues in the same worktree, with
its own model and saved provider session. If all providers are held, the task waits until
the earliest candidate reset. Other tasks continue.

At the reset the scheduler rechecks the held provider before launching another worker.
A still-exhausted window extends the wait. Missing reset data uses `unknown_retry_seconds`,
not a guessed five-hour timestamp. If telemetry is unavailable after that wait, a bounded
worker retry can discover whether quota has recovered. Holds survive manager restarts and
are shown under `provider_holds` in status. They are per run, not a global account ledger;
independent tasks may each encounter a shared account limit once. Provider-level telemetry
may be conservative when model-specific pools differ.

With `return_to_primary` true (the default), a recovered primary is selected between turns.
False keeps the current available provider. Returning to a provider restores its session;
all providers receive the same objective, current worktree, evidence, and handoff. Do not
configure fallbacks unless their account usage is authorized. Authentication failures stay
held until an operator supplies a note after repairing login.

Supervisor models also wait on quota exhaustion and retry the pending question after the
reported reset. Quota failures do not consume the successful-intervention allowance, but
calls remain accounted. Other supervisor failures require operator input.

A **context reset** is separate: `reset_at_tokens` starts a fresh conversation from the
saved handoff when context becomes large. It preserves edits and does not restore account
quota. Supervisor calls are already fresh sessions. The manager never attempts to bypass
provider limits by creating conversations or switching accounts.

## Single-supervisor browser mode

The browser interface uses one persistent supervisor rather than separate batch fleets.
It supports adding/removing agents during execution, an optional local keeper, saved
repository selections, and live completion/blocked signals. See [the browser guide](supervise-web.md).
Supervised workers have full project-file access; legacy `scope`/`protected` entries are
ignored in this mode. Set `options.allow_subagents` to true to allow native delegation.
The setting defaults to false and can be changed in the browser for the next worker turn.
