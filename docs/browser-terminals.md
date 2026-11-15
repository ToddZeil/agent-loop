# Browser terminals for a supervisor interface

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
