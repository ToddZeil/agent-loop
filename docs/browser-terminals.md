# Browser terminals for a supervisor interface

## Lock-ownership prototype and crash experiments

`python3 scripts/probe-tmux-ownership.py` tests a real descriptor handoff on
macOS/POSIX: the controller takes both existing flock locks and sends their open
file descriptions over a local Unix socket to a tmux pane wrapper before closing
its copies. The wrapper retains both locks and starts an interactive shell. The
probe verified the shell could run after handoff, both locks remained unavailable
to another process, and they became available after normal session shutdown.

Additional isolated experiments on this machine:

- `--guardian-only`: killing a separate lock holder released both locks while the
  tmux shell continued running. A guardian alone is not a safe ownership model.
- `--crash-pane`: killing the pane wrapper caused the test shell to exit and the
  locks to be released. This is only one child behavior, not a safety guarantee.
- `--crash-pane --resistant`: a simulated child that closed inherited descriptors
  and ignored SIGHUP stayed alive after the wrapper was killed, while both locks
  were released. This demonstrates a concrete orphan-writer risk.
- `--dual --crash-pane --resistant`: a second holder kept both locks when it alone
  was killed because the pane still held them. After the pane was also killed,
  the simulated resistant child survived without locks. Redundancy reduces a
  single-failure window but is not a complete process-lifetime guarantee.

The probe uses disposable lock files and an isolated tmux server; it kills the
simulated resistant child it starts. It does not control the actual supervisor,
prove browser recovery, or test real providers. The handoff is a more promising
building block than a guardian alone, but production needs a deliberate policy
for orphaned or daemonized children, authenticated reattachment and identity,
startup crashes, cleanup, and lock release before worker scheduling. A process
that deliberately closes inherited descriptors cannot itself be relied on to
retain a POSIX advisory lock. No production backend is enabled by this experiment.

A stronger candidate combines descriptor handoff with a durable ownership record.
Write the record before launching the CLI, and make every worker/terminal entry
point check it while holding the same workspace admission lock. If the owner or
tmux session disappears unexpectedly, treat the worktree as unavailable until
reconciliation proves the writers are gone or an operator resolves it; do not
infer safety just from a free flock. This trades automatic recovery for avoiding
a second writer when an orphan cannot be identified. This is a design proposal,
not implemented or verified by the prototype; it also depends on finding and
covering every worktree writer and defining reliable cleanup on each platform.

The first admission primitives now exist in `loop/worker.py`: a terminal intent
record can be created exclusively while holding the workspace lock, and the
worker, supervisor, terminal and cleanup paths reject a worktree with a record.
An incomplete record also blocks admission. `loop/tmux_session.py` implements an
experimental tmux session lifecycle using that record; a real subprocess test
recovered its session after the launching process exited. The admission lock
and record now use per-worktree Git metadata, so new supervisors in different
homes share them; they also check their own legacy home-local lock. During an
upgrade, an old worker in a different supervisor home does not know about the
new shared lock, so all old writers must be stopped before enabling this design.
The Agent terminal uses this backend when tmux is installed and can opt out for
new sessions with `--no-tmux-terminal`. Returning
control stops tmux and requires explicit operator verification that no terminal
writer remains before clearing the record; a tmux inspection error leaves the
record intact. Recovery for malformed records and a full audit of all worktree
writers remain outstanding. The tmux mode remains experimental.

## Repeatable tmux probe

Run `python3 scripts/probe-tmux-terminal.py` with tmux installed. It creates a
private tmux server and a disposable shell, exercises an alternate screen, resizes
the attached PTY, detaches and reattaches, and verifies the original shell PID,
screen redraw and subsequent input. It kills its private server when finished.
This is a transport and screen experiment, not a managed agent integration.

The supervisor cannot safely wrap its current terminal command in tmux as-is.
`Terminal` and its direct child hold the keeper and workspace locks; a detached
tmux pane can outlive both and continue editing after those locks are released.
Production integration needs a persistent owner that holds both locks for the
entire pane lifetime, durable session identity, safe reattachment after restart,
and explicit teardown before locks are released. Recovery must handle crashes at
each startup and shutdown boundary, and preserve the single-writer invariant.

## Further prototype comparisons (2026-11-15)

These tests used disposable shell processes on macOS arm64 and headless Chromium;
they did not attach to a managed worker or change the running supervisor.

| Configuration | Result | Limit |
| --- | --- | --- |
| ttyd 1.7.7 + Zellij 0.46.0 | Stopping and restarting ttyd preserved the shell PID; reattachment redrew earlier output and accepted new input. After reducing the browser viewport, `stty size` reported `31 110`. | Zellij remained running. Its default startup tip intercepted input on the first attempt; disabling startup tips and using its compact layout allowed the test to run. |
| Independent PTY owner + xterm.js | A disposable Node process held a node-pty shell and a headless xterm 6.0.0 screen, serialized via `@xterm/addon-serialize` 0.14.0. After restarting a separate WebSocket gateway, the browser reconnected to the same PID and restored a simple alternate-screen marker. Resizing from 80x24 to 100x35 reached the shell. | This was a small experimental implementation, not the Python supervisor or a production service. The alternate screen was generated by shell escape sequences, not a real full-screen agent. |
| Existing supervisor | Focused real-PTY shell reconnect/resize/ownership and native agent terminal tests passed. | The tests do not establish survival of a supervisor server restart; the current server owns the PTY master. |

The independent-owner result demonstrates a viable approach for a gateway restart,
but it introduces process discovery, IPC, screen serialization and lifecycle work.
It must retain worktree locks across failures, arbitrate clients, handle slow output,
clean up orphans, and securely authenticate the local IPC before production use.
Neither that owner nor tmux or Zellij preserves a running process after its own owner
or the host dies. Zellij's startup UI also needs explicit configuration for an
embedded single-agent experience. Real agent TUIs, continuous heavy output, owner
crashes, lock recovery and concurrent tabs remain untested for these prototypes.

The separate owner prototype used node-pty for convenience; it is not evidence that
replacing Python's PTY binding is necessary. Zellij was installed through Homebrew;
both experimental sessions and gateway processes were removed after the tests.

## Isolated experiments (2026-11-15)

On this macOS arm64 machine, a headless Chromium browser connected to disposable
loopback ttyd sessions running `/bin/sh`; no supervisor worktree or real agent was
attached. The experiment used ttyd 1.7.7, tmux 3.8, and node-pty 1.1.0.

| Experiment | Observation | Scope |
| --- | --- | --- |
| ttyd with a direct shell | Typing worked; browser reload started a different shell PID. | One shell and reload; no persistent backend. |
| ttyd with tmux | After reload, the shell PID was unchanged, earlier output was redrawn, and new input worked. | One tmux session, one browser tab. |
| Restart ttyd with tmux | After stopping and restarting ttyd, the browser reattached to the same shell PID, redrew earlier output, and accepted new input. | tmux server remained running; this was not a supervisor restart or host restart. |
| node-pty | Spawned an interactive shell, accepted input, and changed `stty size` from `24 80` to `35 100`. | Local PTY API only; no browser transport or process recovery was tested. |

The tmux tests used a dedicated socket and `tmux -L agent-loop-experiment
new-session -A -s probe /bin/sh`; the test socket and processes were removed afterward.
The browser check examined shell PID and terminal WebSocket output, not visual fidelity
of a full-screen application. The first tmux probe sent input before startup completed;
that input did not reach the shell. Waiting for startup resolved it, underscoring the
need for explicit readiness rather than a fixed delay in any integration.

The npm-installed node-pty package's macOS arm64 `spawn-helper` was not executable in
this environment, so spawning initially failed; making that disposable helper executable
allowed the PTY test to pass. This is an observation about this install, not a general
failure-rate claim. tmux and ttyd were installed through Homebrew for the experiment;
the experiment also installed their dependencies and updated Homebrew's CA certificates.

These results support a tmux-backed prototype, not an immediate replacement: the current
supervisor intentionally owns locks and process cleanup, and a persistent tmux server
would change those guarantees. Test actual provider TUIs, resize, alternate screens,
large output, reconnect races, ownership and orphan cleanup before deploying it. Other
options remain architectural comparisons, not hands-on results.

## Reliability review (2026-11-15)

There is no hosted terminal service in this path: xterm.js renders locally in the
browser, and `loop/web.py` owns the POSIX PTY and WebSocket. The code alone cannot
establish the cause of a reported failure. One concrete integration defect was that
the Agent terminal's container was not observed for resizing, so a CLI could retain
the wrong dimensions; both terminal containers are now observed.

The current PTY survives a browser disconnect while the server stays running, but its
bounded byte replay is not a terminal screen snapshot: truncation can begin inside an
escape sequence and full-screen applications may redraw imperfectly. A server restart
loses the PTY master and the process; saved provider chat IDs can resume a conversation,
but cannot restore the live process. Slow clients can exceed the replay buffer and be
disconnected. These are application lifecycle and transport constraints, not evidence
that xterm.js itself is unreliable.

For a next step, retain xterm.js and evaluate a tmux-backed PTY in a prototype if live
process survival across server restarts is required. tmux's separate server owns sessions
and clients can reattach ([tmux documentation](https://github.com/tmux/tmux/wiki/Getting-Started)).
That change must preserve exclusive worktree ownership, cleanup, session identity and
multi-tab arbitration; tmux does not survive its own server or host failure. Test CLI
redraw, resize, server restart, and orphan cleanup before adopting it.

[ttyd](https://github.com/tsl0922/ttyd) provides a packaged browser terminal server but
also uses xterm.js; replacing the gateway with it would add a service and integration
work without automatically solving supervisor ownership or persistent sessions.
[node-pty](https://github.com/microsoft/node-pty) replaces the PTY binding, not the
browser renderer or lifecycle; it adds a Node backend and is more compelling for Windows
support. A different renderer such as [Ghostty's VT core](https://lib.ghostty.org/vt/)
would still need transport, rendering integration and lifecycle management. None is a
demonstrated fix for the observed symptoms without a reproducible failure.

Investigated 2026-11-14. The local interface is now implemented; see [setup and usage](supervise-web.md).
The comparison below records the architecture investigation that informed it.
The target is a terminal running on the same machine as agent-loop, rendered in a browser
at `http://127.0.0.1:<port>`. No SSH connection, hosted shell, or remote workspace is required.
The local server owns the shell/worker processes and the browser displays their output.

The supervisor launches noninteractive provider CLI sessions with structured logs;
browsers cannot type into their closed stdin as if they were interactive terminal sessions.

## A quick local terminal

[ttyd](https://github.com/tsl0922/ttyd) can expose a local shell directly:

```sh
brew install ttyd
ttyd --interface 127.0.0.1 --port 7681 --check-origin --writable /bin/zsh
# Open http://127.0.0.1:7681 in the local browser.
```

This is a standalone shell demonstration, not an attachment to an existing supervisor
worker. For the application, bind each pane to a known task/session rather than launching
another worker on every browser connection. ttyd and tmux were not found on this machine
during the investigation; neither was installed as part of this change.

## Recommendation

Use **xterm.js** for terminal rendering and a structured control API for creating and
managing fleets. Initially stream worker output read-only and send intervention text through
the existing `note` mechanism. Add an independently managed PTY gateway when interactive
shell access is needed. Keep execution ownership in the Python supervisor so browser refresh,
disconnect, or multiple tabs cannot start duplicate coding workers.

[xterm.js](https://xtermjs.org/) is an embeddable browser terminal used by VS Code. It is
not a shell, a worker manager, or a server-side process runner.
[node-pty](https://github.com/microsoft/node-pty) supplies process spawning, read/write,
and resize operations on macOS, Linux, and Windows. It is a backend companion rather
than an alternative renderer. For this Python repository, a Python PTY/WebSocket service
would avoid a Node backend dependency, but requires separate Windows support; node-pty is
the clearer option if cross-platform interactive terminals are a requirement.

## Options

| Option | What it supplies | Fit here |
| --- | --- | --- |
| [xterm.js](https://xtermjs.org/) + a PTY gateway | Embeddable renderer with a separately controlled backend | Best fit for a custom multi-worker interface, layouts, task identities, and access control |
| [ttyd](https://github.com/tsl0922/ttyd) | Ready-made server exposing a command in a browser, based on xterm.js | Quick terminal prototype or authenticated tool pane; process mapping and persistence still belong to the manager |
| [WeTTY](https://github.com/butlerx/wetty) | Browser terminal application using xterm.js and WebSockets | Useful for SSH-oriented remote-machine access; more application to embed than a small terminal component |
| [libghostty-vt](https://lib.ghostty.org/vt/index.html) | Terminal emulation core with WebAssembly distribution | Viable if Ghostty compatibility is a priority; its VT core still needs browser rendering, transport, and session management |

[ttyd's command options](https://github.com/tsl0922/ttyd/blob/main/README.md) include working
directory, authentication/proxy authentication, origin validation, base path, and read-only
versus writable sessions. This makes it a fast way to demonstrate a terminal pane without
building a PTY server first. Browser connections must attach to an existing owned session,
not execute a new coding agent per tab. Wrapping a persistent tmux session can provide that
attachment behavior, but would be an explicit new interactive-worker execution backend.

## Proposed integration

```text
Browser
  ├─ Fleet/task setup, status, decisions, quota countdowns
  │    └─ Control API → Fleet / SupervisedRun
  ├─ Worker activity viewer → read-only event/log stream
  └─ Optional xterm.js shell pane → WebSocket → owned PTY
                                      └─ task worktree
```

A setup form would collect repository, base branch, goal, acceptance checks, provider/model
preferences, parallelism, and limits. The backend validates these into the same manifest as
the CLI. Each task card shows its provider/model, branch, progress, last question and answer,
quota hold/reset time, and next action. Controls cover start, stop scheduling, resume, and
operator notes. Completion links to the commit and diff.

The original `dashboard_server.py` serves read-only state over a Python standard-library HTTP
server. Its existing `/api/state` is not a fleet control API and does not provide WebSockets
or PTYs. A control extension needs explicit fleet registration (across repositories), create,
status, note, stop/resume endpoints, plus a bounded log stream. SSE is sufficient for status
and logs; use WebSockets when bidirectional PTY input and resizing are actually needed.

For real interactive provider terminals, add a PTY-aware worker adapter with durable ownership,
output capture, resize, reconnect, and input arbitration. The current provider JSON protocol
should continue to drive decisions; scraping a rendered screen is a fragile replacement.
A shell pane must not silently introduce a second writer while a coding worker owns its tree.
Explicitly pause/reconcile the worker before enabling manual edits in that worktree.

## Access and reconnect behavior

Terminal access executes on the server host; a browser terminal does not execute the worker
on the user's phone or browser. The browser must reach the machine holding the repository.
Start with loopback binding and same-origin access. Any remote deployment needs authenticated,
authorized task access and origin checks. Reconnection should restore a bounded transcript
and attach to the existing session; disconnecting should not kill the task. Treat output as
terminal data rather than executable HTML, and apply backpressure to log/PTY streams.
These requirements follow the [xterm.js integration security guide](https://xtermjs.org/docs/guides/security/).

A full frontend can therefore be built around the existing manager without making terminal
emulation responsible for planning, quota recovery, or worker lifecycle.

## Implemented choice

`loop/web.py` supplies a separate loopback aiohttp server and POSIX PTY backend.
`web/` uses locally installed xterm.js and its fit addon. Fleet controls use JSON HTTP;
PTY input/output uses WebSockets. The original dashboard remains independent.
