# UI simplification validation — 14 November 2026

## Release validation — 15 November 2026

Running Playwright outside the sandbox resolves the Chromium launch restriction.
The first executable run passed 12 tests and exposed a shared-fixture assumption:
the visual scenario counted only its two new workers, ignoring workers created
by earlier tests. It now records the initial count and checks two additions.
The subsequent full browser suite passed **13 tests (1.2m)**, recorded in
`.agent-loop/ui-review/browser-release.log`. The isolated visual scenario also
passed, capturing an actually empty workspace followed by two workers.

Three visual review/refinement cycles were completed by opening the retained
screenshots, rather than relying on test outcomes alone:

| Cycle | Inspected views | Findings and refinement |
| --- | --- | --- |
| 1 | 1440×1000 empty workspace and populated conversation; full-suite ten-worker desktop capture | Primary Add agents action and conversation hierarchy are clear; composer fits within the desktop viewport. Corrected the shared-fixture count and captured a fresh two-worker scenario for the next review. |
| 2 | Two-worker phone captures at 390px and 320px, collapsed and expanded Agent settings; Add agents, Supervisor settings and Projects dialogs | Inspector stacks below both cards without horizontal overflow. Model selectors were too narrow in two-column phone forms. Stacked dialog fields and agent model/effort labels vertically below 420px. Before-refinement captures retained in `.agent-loop/ui-review/before-phone-refinement/`. |
| 3 | Refined 320px Add agents and Supervisor settings, expanded Agent settings; desktop Add agents and phone Projects | Model values are now readable at phone widths, headings wrap, and desktop forms keep their original columns. Long phone dialogs scroll vertically; added assertions and captures for footer action reachability, in addition to overflow and dismissal checks. Refined isolated scenario passed in 14.5s (`browser-visual-refined.log`). |

Screenshots are retained as `.agent-loop/web-redesign-*.png`; these include
desktop, both phone widths, expanded settings and each dialog. The historical
blocked runs below are superseded by the executable browser results above.
Final full browser run: **13 passed (1.1m)**, including phone dialog footer
reachability, retained in `.agent-loop/ui-review/browser-release-final.log`.
Final full Python run outside the sandbox: **344 tests in 493.863s, OK**, with
no skips, retained in `.agent-loop/ui-review/unittest-release.log`. The real
process identity integration case executes successfully outside the sandbox.

## Latest retry — 15 November 2026

Reran all 13 browser tests with the installed Chromium. All 13 failed before
assertions at the same macOS MachPort permission error (1100). Output is retained
in `.agent-loop/ui-review/browser-latest.log`; browser validation and all three
visual review cycles remain incomplete. No new screenshots or visual findings
are claimed. A browser-capable host or removal of the host restriction is still
required.

The focused continuous-worker suite passed: **18 tests, OK (skipped=1)**,
recorded in `.agent-loop/ui-review/continuous-latest.log`. JavaScript syntax
checks and `git diff --check` passed. The completed full Python result below
remains the latest full-suite evidence.

The previous preview was no longer responding. Restarted the isolated worktree
preview on `http://127.0.0.1:8778` and verified its served `app.js` matches the
worktree. No commit, branch change, or merge was performed.

Changes remain uncommitted. Conversation is the first tab, model/effort controls
live inside Agent settings, empty Removed agents navigation is hidden, and tabs
support arrow keys and Home/End.

## Browser evidence correction

The earlier claim that 17 web tests passed is unsupported and must not be used
as acceptance evidence. The current browser suite contains **13 tests**. All 13
failed at browser launch on this host, before executing their assertions.

Installed Playwright Chromium v1254 in `.agent-loop/playwright` and retried:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.agent-loop/playwright" npm --prefix web test
```

Chromium aborts with `bootstrap_check_in org.chromium.Chromium.MachPortRendezvousServer…:
Permission denied (1100)`. System Google Chrome also aborts at launch. This
host restriction prevents a completed browser run and screenshot capture.
The complete retry log is `.agent-loop/ui-review/browser.log`; traces and
error contexts are in `.agent-loop/web-test-results/`.

The desktop conversation test now checks the inspector's top edge is at or below
the agent list's bottom edge at **390px and 320px**. It also retains overflow and
conversation visibility assertions, and writes separate screenshots for both
widths when run on a working host. These new assertions have not executed here.

The repair retry also fails at the same Chromium launch step. Its full-suite
output is `.agent-loop/ui-review/browser-current.log`; the focused desktop
retry failed before executing any assertions as well. No browser pass is claimed.
All **13 tests** in the full repair retry failed before their assertions ran.

The screenshot scenario now creates **two workers in separate repositories**,
checks search filtering and restores the complete list before checking mobile
stacking. It captures the empty desktop workspace, populated desktop workspace,
both phone widths with settings collapsed and expanded, and Add agents,
Supervisor settings and Projects dialogs at all three widths. It also checks
dialog horizontal overflow and dismissal. These captures are prepared for a
working host; they are not completed visual reviews.

## Visual review cycles still required

No visual cycle is claimed complete. No screenshots were generated in this run.
The following reviews require a host that permits Chromium to launch:

| Cycle | Views to capture and inspect | Findings / refinement status |
| --- | --- | --- |
| 1 | Desktop 1440×1000: empty workspace, populated workers, conversation and composer | Blocked before rendering; assess hierarchy, reading space and primary actions. |
| 2 | Phone 390×844 and 320×844: populated list, selected inspector and settings | Blocked before rendering; stacking assertion added, inspect wrapping, scrolling and tap targets. |
| 3 | Desktop and phone: Add agents, Supervisor settings, Projects dialogs, then populated workspace | Blocked before rendering; inspect dialog reachability, dismiss controls and regressions after refinements. |

Retain captures and record actual findings and subsequent changes for every
cycle before accepting the visual work. Existing screenshot-producing tests
are not a substitute for inspecting those screenshots.

## Python failure investigation

The original failing case was
`test_reused_pid_is_not_mistaken_for_the_worker_group`. This sandbox denies `ps`,
so `process_identity()` returns an empty string. The test then expected a PID
mismatch to be detected without any process metadata. Production code deliberately
preserves ownership conservatively when that metadata is unavailable.

The real-process integration case now explicitly skips when identities are
unavailable. A separate deterministic test verifies that a known mismatch returns
false without signalling the process, and that unavailable metadata still checks
process-group existence. Production process ownership behavior is unchanged.
Focused result: **18 tests, OK (skipped=1)**, retained in
`.agent-loop/ui-review/continuous.log`.

The initial full run completed: **343 tests in 405.277s, FAILED (failures=1)**,
with the PID identity assertion above as the sole failure. Its log is retained
in `.agent-loop/ui-review/unittest-before.log`.

Final full run: `.agent-loop/web-venv/bin/python -m unittest discover -s tests`
completed **344 tests in 371.806s, OK (skipped=1)**. The one skip is the real
process identity integration case described above; the new deterministic case
passed. Full output is `.agent-loop/ui-review/unittest-final.log`.
`git diff --check`, `node --check web/app.js`, and
`node --check web/tests/workspace.spec.cjs` also passed.

Repair verification reran the full unittest discovery command: **344 tests in
322.103s, OK (skipped=1)**. The complete new output is retained in
`.agent-loop/ui-review/unittest-current.log`. The same process identity
integration case is skipped, and its deterministic counterpart passes.

## Local preview

An independent instance serves this worktree at `http://127.0.0.1:8778`, with
state isolated in `.agent-loop/ui-review/local-web`. Port 8777 was occupied;
the existing instance was not restarted. HTTP checks confirmed that `/app.js`
and `/styles.css` match the worktree bytes. Browser files are served directly,
so no separate build is needed. No commits, branch changes or merge were made.
