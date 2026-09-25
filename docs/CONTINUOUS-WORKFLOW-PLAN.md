# Continuous worker workflow

Status: proposed implementation plan. This document does not change or launch a runner.

## Goal

Complete long-running objectives with fewer model handoffs, preserve useful work through
interruptions, and recover from ordinary failures without an operator restarting the run.
Measure success by completed objectives, total cost, elapsed time, and human interventions.

The default workflow will be one continuing coding worker in an isolated worktree, managed
by a small Python keeper. The worker owns discovery, its working plan, implementation, tests,
and repair. A local Ollama model assists with summaries and failure classification. A cloud
reviewer is invoked only at configured milestones or when progress stalls.

Implement this by evolving Lite and reusing the existing durable worker boundary. Avoid
introducing a fourth runner alongside the strict engine, Lite, and the Muse script.

## Why change the current workflow

The strict planner requires precise file lists and existing checks before implementation.
Failed checks reset the worktree and leave another worker to reconstruct the attempt from
saved files. Lite retains edits but still launches fresh workers under a model supervisor.
Both approaches add overhead to ordinary implementation and repair.

The review also identified concrete reliability gaps:

- `loop run` exits when work is parked; `Service.eligible()` excludes parked objectives.
- Lite lets errors from action execution and provider launch escape its main loop. The
  existing test harness reproduced a malformed `wait.seconds` and a launch `OSError`
  terminating execution while the objective remained unfinished.
- Five unchanged failed checks with different dispatch prompts produced zero stagnant
  turns in the same harness. Action text is an unreliable measure of progress.

These observations establish failure mechanisms, not their frequency in production.
The available local run history is insufficient to quantify savings from a replacement.

## Normal workflow

1. Start with a goal, repository scope, provider preferences, and completion criteria.
   Prepare one isolated worktree and record its base commit. Run configured setup once.
2. Launch a capable worker. It inspects the repository, keeps a short editable checklist,
   and works on a coherent change, including any relevant implementation and test files.
3. Let the worker run focused checks and repair failures within its conversation.
4. When a turn ends, the keeper records the result, session identity, usage, files, and
   diagnostics. Preserve unfinished edits. Run independent checks at milestone or completion
   boundaries; feed failures back to the continuing worker.
5. Resume the same worker session while productive. If a provider cannot resume, start a
   new session from the goal, checklist, worktree, and evidence-backed handoff notes.
6. Finish only when the objective's completion criteria have been verified against the
   current tree. Produce the branch and completion report; publication remains separate.

A returned model response is a turn boundary, not proof that the objective is complete.
The worker can adjust its checklist without generating a replacement execution plan.

## Responsibilities

| Component | Owns |
| --- | --- |
| User objective | Goal, scope, protected paths, completion criteria, optional spending limits |
| Coding worker | Discovery, checklist, implementation, diagnostics, tests, local repair |
| Python keeper | Process ownership, durable state, recovery, waits, verification, checkpoints, accounting |
| Local Ollama model | Concise handoffs and advisory failure hints after completed turns |
| Optional cloud reviewer | Milestone quality review or diagnosis of stalled work |

Remove the mandatory cloud supervisor and its JSON action protocol from the new default
path. Keep the initial plan inside the worker's conversation and persisted checklist.
Do not require a scout call, provider rotation, per-file task decomposition, or an audit
after every small edit.

Keep scope enforcement at the objective level: allowed repository roots and protected
surfaces, with one active worker per worktree. A worker may discover and create files inside
that scope. Only the keeper finalizes accepted commits.

The local model remains optional. Use the configured Ollama model, currently defaulting to
`qwen2.5-coder:7b`. Bound its processing time; if it is slow, unavailable, or returns invalid
output, fall back to deterministic notes. It cannot declare completion, authorize commands,
change scope, or decide retries. Preserve raw evidence alongside summaries.

## Recovery contract

Use a small set of durable states: `ready`, `working`, `verifying`, `waiting`, `paused`,
and `complete`. Recovery is a keeper operation that reconciles these states on restart.
`paused` represents an explicit operator pause. `waiting` always records its reason and
either a retry time, a probe condition, or a required operator action.

| Event | Required behavior |
| --- | --- |
| Failed implementation check | Preserve edits; return exact diagnostics to the same worker |
| Known quota reset | Wait until reset, or use an eligible configured fallback; preserve session and work |
| Unknown quota reset | Back off and probe; avoid repeated paid calls to test availability |
| Temporary launch or transport error | Record it, back off, and resume without resetting coding progress |
| Idle or hard timeout | Stop and reap the owned process group, preserve files, then resume or diagnose |
| Keeper restart | Reconcile the existing attempt and result before launching any replacement |
| Context exhausted or unusable session | Start a fresh session with durable notes, diagnostics, and existing files |
| Repeated coding failure | Diagnose once and change strategy; do not keep rephrasing identical instructions |
| Missing credentials or external prerequisite | Enter waiting with a cheap probe or explicit action needed |
| Explicit spending limit reached | Enter waiting until the limit resets or the operator changes it |
| Invalid state, unexpected Git history, or uncertain ownership | Preserve evidence, suspend dispatch, report the exact condition |
| Explicit stop | Save state and stop cleanly; a later resume continues the objective |

One layer owns each retry: the keeper. Provider adapters return classified results and
deadlines. The operating system restarts a crashed keeper; it does not start another worker
before ownership reconciliation. Model calls are unnecessary while waiting.

Handle expected process, provider, and validation failures at the iteration boundary.
Unexpected exceptions must produce a visible fault with a traceback and suspend dispatch,
not disappear into an unlimited retry loop. If state cannot be written, report the failure
to stderr and avoid launching more work until durable storage recovers.

## Continuity and progress

Persist the objective configuration, state version, provider/model/session identity, active
attempt identity, process ownership, retry condition, accepted commit, checklist, handoff,
check evidence, failure history, and usage. Reuse atomic storage and ownership mechanisms
already in `loop/storage.py`, `loop/worker.py`, and the engine's attempt reconciliation.

Store runtime control records outside the worker's editable surface. The worker's checklist
and progress notes are advisory; they cannot alter the objective or keeper state.

Preserve modified, deleted, and untracked in-scope files during interruptions and provider
handoffs. An unfinished worktree is a recoverable checkpoint. It is distinct from an accepted
commit, which requires the configured milestone checks. Ordinary failed tests do not cause
a rollback. Recovery snapshots must survive keeper restarts and must not include ignored
dependency trees by default.

Separate activity from progress. Output and file writes show activity for idle timeouts.
Progress requires evidence such as a previously failing assertion passing, a verified
prerequisite being resolved, or a milestone meeting its criteria. A different prompt, larger
diff, new commit, changed task name, or a worker's success claim is insufficient by itself.
Discovery may justify continued work through concrete findings without producing a diff.

Initial recovery policy to validate in trials:

- Allow a worker to repair an ordinary failed check in its existing session.
- After two completed attempts with the same normalized failure and no verified progress,
  request one diagnostic review or select a configured stronger worker with explicit feedback.
- Preserve that history across sessions, model switches, and checklist edits. Permit one
  evaluated recovery strategy for unchanged evidence; further inference requires new evidence
  or an operator instruction.
- If recovery makes no progress, enter waiting with the cause and the condition needed to
  resume. Continue cheap relevant probes for external blockers. Do not manufacture activity
  merely to keep the process looking busy.

These limits apply to repeated ineffective approaches, not the lifetime of the objective.
Productive work may continue through many turns. Missing or unparseable check output must
not be counted as evidence of progress.

## Verification and cost

Keep completion criteria separate from the worker's mutable checklist. Existing criteria
cannot be dropped or weakened by a worker. Passing a pre-existing test suite is regression
evidence; it does not by itself prove that a requested feature or document was delivered.

Workers may write implementation tests, while keeper-owned acceptance checks and their
supporting files remain protected. New criteria discovered during work can be proposed and
reviewed before becoming acceptance gates. Configure substantive review for outputs that
cannot be verified mechanically. If a required reviewer is unavailable, wait; do not treat
unavailability as approval. Reuse review evidence only for the exact unchanged tree.

Run focused checks during development, relevant independent checks at milestone boundaries,
and full configured completion checks before declaring success. Do not require the final
objective gate to pass before preserving partial work. Deduplicate identical checks within
one verification pass and invalidate prior acceptance evidence when the checked tree changes.

Choose the default worker for a reasonable chance of completing the objective. Use cheaper
models for suitable work once measured results support the choice. Avoid a mandatory climb
through weak models and avoid switching providers merely to consume expiring quota.

Measure every worker, reviewer, and local-model invocation, including failures and unknown
usage. Report cloud tokens and available cost estimates separately from local latency.
Track completed objectives, total elapsed time, waits, repeated failures, context restarts,
and operator interventions. Preserve existing explicit budgets through migration.

## Implementation sequence

### 1. Establish durable recovery

Work primarily in `loop/lite.py`, `loop/worker.py`, `loop/storage.py`, and the CLI lifecycle.
Reuse or extract the engine's attempt ownership and reconciliation rather than implementing
a second process manager. Version state and persist launch intent before spawning.

Acceptance: injected launch errors, malformed responses, timeouts, and keeper termination
preserve work and yield a recoverable state. Restart after spawn, after result creation,
and after commit causes neither duplicate workers nor duplicate acceptance. A descendant
process cannot remain active while a replacement edits the worktree.

### 2. Make one continuing worker the default

Replace Lite's normal supervisor/dispatch round trip with a worker continuation loop.
Extend adapters with explicit resume capabilities and persist worker session identities.
Check each installed provider's actual resume behavior before enabling that capability;
use a fresh session with the same worktree and handoff when unsupported or invalid.

The worker receives the objective, scope, completion criteria, checklist, previous evidence,
and relevant operator notes. Use a minimal optional turn result for continue, completion
claim, or blocker. Malformed model output must not become a keeper exception or trusted
success. The keeper owns scheduling and verification regardless of response format.

Acceptance: a task requiring implementation, a failed test, and repair retains its edits
and session across turns where supported. A provider switch preserves the same objective
and files. Straightforward work needs no separate planning or cloud supervisor call.

### 3. Add evidence-based recovery and verification

Adapt `loop/diagnosis.py`, keeper decisions, existing verification, and failure records.
Classify environment/provider problems separately from coding failures. Replace action-text
stagnation detection with normalized diagnostics and independently observed progress.
Keep local-model hints advisory and make cloud diagnosis exceptional.

Acceptance: five unchanged failures with different prompts trigger bounded recovery, then
waiting. A resolved prerequisite or improving check result permits continued work. Quota
waits consume no coding retry allowance. A false completion claim, dropped checklist item,
or passing baseline check cannot bypass the objective's acceptance criteria. Ollama failure
does not interrupt execution.

### 4. Migrate and consolidate

Keep the existing `lite start/resume/status/tail/note` interface during rollout. Expose the
continuous workflow as an opt-in version of Lite until validated, then make it the default
for new Lite runs. Status must show the actual worker, progress evidence, wait reason, and
next recovery action. Update the dashboard, README, and relevant repository skills.

Do not silently reinterpret an active run. Migration requires a quiescent worktree with no
owned worker, a backup of its control state, and a recorded schema transition. Carry forward
the original goal, commits, edits, gates, budgets, failures, and usage. Treat old pending
tasks as checklist material. Do not resume a supervisor conversation as a worker session.

Keep old run records readable and resumable through their compatible path. Rollback uses
the saved configuration and state under exclusive ownership, retaining any newer code and
evidence for reconciliation. It must never reset a worktree to an older checkpoint silently.

The strict JSON engine remains available for existing runs during migration. After trials,
deprecate it for new general coding objectives. Route shared recovery through one implementation
and retire duplicate scheduling code only after compatibility is covered. Keep learning,
self-improvement, and automatic publishing outside the new default execution path.

Acceptance: an interrupted old run can resume through its original path; an explicitly
migrated run retains its scope, gates, edits, usage, and failure history. New runs use one
worker loop without invoking the legacy cloud supervisor.

### 5. Validate on real long-running objectives

Use isolated copies of the same starting commits and identical acceptance criteria to
compare the current workflow with the revised workflow. Include a cross-file feature,
a debugging task, and a multi-batch content or data task. Record incomplete and waiting
runs alongside successes; do not omit failed attempts from cost figures.

First run deterministic fault tests using fake providers and clocks: quota waits, missing
dependencies, invalid sessions, keeper crashes, worker crashes, repeated failed checks,
unavailable Ollama, and failure during verification. Then run an unattended trial with real
providers, checking continuation across several turns and at least one controlled restart.

Promotion requires no lost edits, no duplicate active workers, no false completion, and
recovery from every injected recoverable fault. Compare quality, completion rate, total
cost, elapsed time, and interventions on the real workload. Do not claim cost savings from
mock tests or the tiny clamp smoke test. Keep the revision opt-in if the trials do not show
an improvement; adjust the worker choice or recovery policy using the recorded evidence.

## Deliverable

A user supplies a goal and can leave the runner working. The worker plans and repairs within
that goal, the keeper survives ordinary interruptions, and local summaries preserve useful
context. Completion is verified. A true blocker remains visible and resumable with a specific
next action, rather than ending the objective or consuming unlimited model calls.
