"""Run a bounded, hourly-researched LiteRun with an independent watchdog.

Usage: python3 scripts/seo_lite.py prepare --repo /path/to/your-pt.com --id ID
       python3 scripts/seo_lite.py start /path/to/control.json
The start command detaches a watchdog; its child owns the ordinary LiteRun lock.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import shlex
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from loop.lite import LiteRun, git
from loop.storage import atomic_json


class SeoLiteRun(LiteRun):
    """Honor the selected providers and tolerate locally excluded research files."""
    def telemetry(self, provider):
        cached = self._limits_cache.get(provider)
        if cached and self.clock() - cached[0] >= 120:
            self._limits_cache.pop(provider, None)
        return super().telemetry(provider)

    def available(self, candidates, state, exclude=None):
        choices = super().available(candidates, state, exclude)
        if self.config.get('prefer_claude'):
            choices.sort(key=lambda provider: {'claude': 0, 'codex': 1}.get(provider, 2))
        return choices

    def supervisor_turn(self, state):
        choices = self.available_supervisors(state=state)
        previous = state.get('last_supervisor_provider')
        next_provider = next((provider for provider in choices if provider != previous),
                             choices[0] if choices else state['provider'])
        sessions = state.setdefault('supervisor_sessions', {})
        fields = ('session_id', 'turns_in_session', 'context_tokens', 'stagnant_turns',
                  'consecutive_errors', 'escalated')
        if state['provider'] != next_provider:
            sessions[state['provider']] = {field: state.get(field) for field in fields}
            prior = sessions.get(next_provider, {})
            state.update(provider=next_provider,
                         session_id=prior.get('session_id'),
                         turns_in_session=prior.get('turns_in_session', 0),
                         context_tokens=prior.get('context_tokens', 0),
                         stagnant_turns=prior.get('stagnant_turns', 0),
                         consecutive_errors=prior.get('consecutive_errors', 0),
                         escalated=prior.get('escalated', False))
            self.journal('supervisor_rotation', provider=next_provider,
                         previous_provider=previous)
        result = super().supervisor_turn(state)
        sessions[next_provider] = {field: state.get(field) for field in fields}
        state['last_supervisor_provider'] = next_provider
        return result

    def dispatch(self, state, action):
        choices = self.available(self.config['workers'], state)
        if self.config.get('prefer_claude') and choices:
            action = {**action, 'provider': choices[0]}
        # Ordinary Lite worker prompts carry only task instructions. Carry the
        # operator's standing scope and billing instructions into every attempt.
        action = {**action, 'prompt': str(action.get('prompt') or '') +
                  '\n\nSTANDING OPERATOR INSTRUCTIONS:\n' + self.path('goal.md').read_text()}
        return super().dispatch(state, action)

    def commit(self, task_id, paths):
        """Avoid git-add fatal errors for ignored files already removed from the index."""
        safe_paths = []
        for name in paths or []:
            tracked = subprocess.run(['git', '-C', str(self.worktree), 'ls-files',
                                      '--error-unmatch', '--', name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ignored = subprocess.run(['git', '-C', str(self.worktree), 'check-ignore',
                                      '-q', '--', name])
            if tracked.returncode == 0 or ignored.returncode != 0:
                safe_paths.append(name)
        return super().commit(task_id, safe_paths)


def read(file, default=None):
    try:
        return json.loads(Path(file).read_text())
    except FileNotFoundError:
        return default


def spent(ledger):
    data = read(ledger, {"entries": [], "reservations": []})
    return sum(float(e.get('costUsd', e.get('estimatedCostUsd', 0)))
               for e in data['entries'] + data['reservations'])


def stop_reason(control, now=None):
    if (time.time() if now is None else now) >= control['deadline']:
        return control.get('deadline_reason', 'eight_hour_deadline')
    minimum_free = control.get('minimum_free_bytes', 0)
    if minimum_free and shutil.disk_usage(control['run_dir']).free < minimum_free:
        return 'low_disk_space'
    used = spent(Path(control['run_dir']) / 'dataforseo-spend.json')
    if not control.get('stop_on_budget', True):
        return None  # Paid collector still enforces its independent hard cap.
    if used >= control['budget_usd'] - 1e-9:
        return 'dataforseo_budget'
    if read(Path(control['run_dir']) / 'latest-research.json', {}).get('stop') == 'run_budget':
        return 'dataforseo_budget'
    return None


def descendants(root, pairs):
    found = {root}
    while True:
        new = {pid for pid, parent in pairs if parent in found} - found
        if not new:
            return found
        found |= new


def process_tree(root):
    out = subprocess.check_output(['ps', '-axo', 'pid=,ppid='], text=True)
    return descendants(root, [tuple(map(int, line.split())) for line in out.splitlines()])


def terminate(pids):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(pids, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(1)


def prepare(args):
    repo = Path(args.repo).resolve()
    run = LiteRun(repo, args.id)
    collector = Path(__file__).with_name('seo_lite_collect.mjs')
    control_path = run.path('bounded-control.json')
    goal = f'''Continue SEO improvements for your-pt.com on top of the reviewed changes in PR #254.
This Lite run ends at {args.deadline_local} Pacific/Auckland or US ${args.budget_usd:.3f} in
DataForSEO charges, whichever comes first. Claude and Codex both supervise and can implement;
the Python keeper uses the local Ollama model qwen2.5-coder:7b.
The host collector owns ALL paid research and both spend ledgers. Never call DataForSEO,
SerpAPI, LLM APIs, paid web search, or other metered services from a worker; never change
credentials, budget files, the collector, watchdog, tests, validators, gates, robots or release policy.
Subscription-backed CLI inference is permitted; purchased API/overage credits are not.

Read .agents/skills/seo-preservation/SKILL.md completely, AGENTS.md, and the existing
docs/seo/continuous-seo-ledger.md. Follow the repository's content evidence, AI review and
citation rules. The operator specifically selected the Lite workflow (host Python keeper,
supervisor, workers); do not start another autonomous workflow or delegate subagents.

At each new research cycle inspect docs/seo/reports/{args.id}-cycle-*.json and
docs/seo/gsc/{args.id}-query-page.json. Use collected market, language and device evidence
carefully; separate property-wide GSC evidence from locale-specific SERP results. Prioritize
incumbent pages with real impressions, positions 4-30, weak CTR, unmet intent,
missing useful inlinks, content/schema mismatch, or technical defects. Read SERP competitors
and the actual incumbent; make substantive useful improvements, never keyword stuffing or
mass boilerplate. Preserve strong metadata unless an evidence-backed change helps intent.
Add a new page only when distinct query intent and evidence justify it; otherwise improve
existing pages and technical SEO. Avoid changing
medical/safety claims unless primary sources actually support them and independent AI
review is recorded. Never invent references, review credentials or ranking improvements.

First establish the evidence baseline and commit research artifacts with a ranked, specific
refresh queue and baseline checks. Then implement small, coherent refreshes and relevant
internal links, record before/after reasoning and provenance in the continuous SEO ledger,
and run focused tests plus the keeper's gates. Build/render affected pages before accepting
content changes; check canonical, metadata, headings, visible schema parity and links.
Do not add draft/noindex or remove useful content/links to make checks pass.
Workers must not edit gate/check scripts. Inspect actual diffs and reject generic prose or
unsupported claims. Keep changes on this isolated branch. Once substantive work passes focused
and full release checks and receives independent review from the other model provider, publish
a stacked PR targeting the PR #254 branch. Do not merge, deploy, submit indexing requests, send
outbound messages or mutate production.

For a new hourly cycle, keep all earlier improvements and re-evaluate newly collected
queries; do not repeatedly rewrite the same page without new evidence. Use done when a
cycle's safe useful work is exhausted; the host waits locally and reopens it for the next
hourly collection. Do not burn model turns waiting. The watchdog stops at the deadline
even if a worker is active, preserving partial work for review. Final reports must distinguish
measured evidence from hypotheses and identify unfinished work and checks.
'''
    options = {
        'supervisors': ['claude', 'codex'], 'workers': ['claude', 'codex'],
        'provider_order': 'least_used', 'supervisor_home': None,
        'supervisor_models': {'claude': 'sonnet', 'codex': 'gpt-6.1-sol'},
        'supervisor_escalation': {'claude': 'opus'},
        'catalog': [
            {'provider': 'codex', 'tier': 'light', 'model': 'gpt-6-luna', 'effort': 'low'},
            {'provider': 'codex', 'tier': 'standard', 'model': 'gpt-6-luna'},
            {'provider': 'codex', 'tier': 'strong', 'model': 'gpt-6.1-sol'},
            {'provider': 'claude', 'tier': 'standard', 'model': 'sonnet'},
            {'provider': 'claude', 'tier': 'strong', 'model': 'opus'}],
        'audit': {'enabled': True, 'required': True, 'sample': 1.0},
        'gates': [shlex.join([sys.executable, str(Path(__file__).resolve()), 'guard', str(control_path)]),
                  'npm run test:unit', 'npm run content:validate:release',
                  'node scripts/check-future-page-citation-gate.mjs'],
        'supervisor_max_seconds': 600, 'worker_max_seconds': 1800,
        'supervisor_idle_seconds': 180, 'worker_idle_seconds': 600,
        'check_timeout_seconds': 600, 'unknown_retry_seconds': 300,
    }
    run.create(goal, base=args.base, options=options)
    exclude = Path(git(repo, 'rev-parse', '--git-path', 'info/exclude'))
    if not exclude.is_absolute():
        exclude = repo / exclude
    with exclude.open('a') as stream:
        stream.write('\n/node_modules\n')
    # Use existing dependencies without copying secrets or unrelated checkout changes.
    os.symlink(repo / 'node_modules', run.worktree / 'node_modules')
    # Only public build settings and read-only GSC auth; paid provider keys stay out.
    lines = (repo / '.env').read_text().splitlines()
    allowed = ('PUBLIC_', 'GOOGLE_SEARCH_CONSOLE_', 'SITE_URL', 'NODE_ENV')
    env = '\n'.join(line for line in lines if '=' in line and
                    line.split('=', 1)[0].strip().startswith(allowed)) + '\n'
    secret_file = run.worktree / '.env'
    secret_file.write_text(env)
    secret_file.chmod(0o600)
    # Preserve existing ledger/cache; the collector always addresses the main ledger.
    control = {'id': args.id, 'repo': str(repo), 'worktree': str(run.worktree),
               'run_dir': str(run.dir), 'budget_usd': args.budget_usd,
               'duration_seconds': args.duration_seconds, 'deadline': args.deadline,
               'started_at': time.time(),
               'collector': str(collector), 'interval_seconds': 3600,
               'base_sha': git(repo, 'rev-parse', args.base),
               'continuous_improvement': True, 'allow_new_pages': True,
               'locales': ['en', 'de', 'fr', 'es', 'ja', 'ko', 'it', 'zh', 'pt', 'ar', 'hi', 'id'],
               'queries_per_cycle': 2,
               'stop_on_budget': True, 'minimum_free_bytes': 1610612736,
               'deadline_reason': 'five_pm_nzdt_deadline'}
    atomic_json(control_path, control)
    run.path('launch-dirty-scope.txt').write_text(git(repo, 'status', '--short') + '\n')
    print(control_path)


def child(control_path):
    control = read(control_path)
    # Prevent ambient API-key billing; use the CLIs' existing subscription sessions.
    for key in list(os.environ):
        if key.startswith(('ANTHROPIC_', 'OPENAI_', 'DATAFORSEO_', 'SERPAPI_')):
            os.environ.pop(key)
    run = SeoLiteRun(control['repo'], control['id'])
    # Clamp quota sleeps and CLI calls to the host deadline too.
    run.sleep = lambda seconds: time.sleep(max(0, min(seconds, 60, control['deadline']-time.time())))
    original_invoke = run.invoke
    def bounded_invoke(argv, cwd, timeout, **kwargs):
        remaining = control['deadline'] - time.time()
        if remaining <= 0:
            raise TimeoutError('SEO run deadline reached')
        return original_invoke(argv, cwd, min(timeout, remaining), **kwargs)
    run.invoke = bounded_invoke
    schedule_file = run.path('research-schedule.json')
    schedule = read(schedule_file, {'next_at': 0})
    while not stop_reason(control):
        state = run.load('supervisor.json')
        if time.time() >= schedule['next_at']:
            proc = subprocess.run(['node', control['collector'], str(control_path)],
                                  cwd=ROOT, timeout=min(600, control['deadline']-time.time()),
                                  capture_output=True, text=True)
            with run.path('collector.log').open('a') as log:
                log.write(proc.stdout + proc.stderr)
            schedule['next_at'] = time.time() + control['interval_seconds']
            atomic_json(schedule_file, schedule)
            state['finished'] = False
            state['notes_for_supervisor'].append(
                'New hourly research cycle: inspect the fresh reports and raw SERPs; '
                'incorporate these artifacts into an evidence-backed refresh task. Collector: '
                + (proc.stdout or proc.stderr)[-3000:])
            run.save('supervisor.json', state)
        if state['finished']:
            if control.get('continuous_improvement'):
                run.sleep(60)
                state['finished'] = False
                state.update(session_id=None, turns_in_session=0, context_tokens=0)
                state['notes_for_supervisor'].append(
                    'Continue the authorized improvement program: choose a different '
                    'language, incumbent gap, new-page intent or technical SEO route. '
                    'Use GSC, existing research and free primary sources. '
                    'Do not wait for paid research or repeat an exhausted plan.')
                run.save('supervisor.json', state)
            else:
                run.sleep(min(60, max(0, schedule['next_at']-time.time())))
        else:
            run.run(max_turns=1)


def guard(control_path):
    control = read(control_path)
    tree = control['worktree']
    names = set(git(tree, 'diff', control['base_sha'], '--name-only').splitlines())
    names.update(git(tree, 'ls-files', '--others', '--exclude-standard').splitlines())
    protected = ('scripts/', 'test/', 'tests/', '.agents/', '.github/', '.env', 'AGENTS.md',
                 'package.json', 'package-lock.json', 'firebase.json', '.firebaserc')
    invalid = [name for name in names if name.startswith(protected)]
    if not control.get('allow_new_pages', False):
        added = git(tree, 'diff', control['base_sha'], '--diff-filter=A', '--name-only').splitlines()
        invalid += [name for name in names if name.startswith('src/content/') and
                    (name in added or not git(tree, 'ls-files', '--', name))]
    if invalid:
        raise ValueError('Protected files or new content URLs: ' + ', '.join(sorted(set(invalid))))
    if spent(Path(control['run_dir'])/'dataforseo-spend.json') > control['budget_usd'] + 1e-9:
        raise ValueError('Run spending exceeded the cap')
    print('Protected scripts, policies, authorized content scope and spend guard passed.')


def watch(control_path):
    control = read(control_path)
    lock = Path(control['run_dir']) / 'watchdog.lock'
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if control['deadline'] is None:
            control['started_at'] = time.time()
            control['deadline'] = control['started_at'] + control['duration_seconds']
            atomic_json(control_path, control)
        status_file = Path(control['run_dir']) / 'bounded-status.json'
        status = {'status': 'running', 'watchdog_pid': os.getpid(),
                  'deadline': control['deadline'], 'budget_usd': control['budget_usd']}
        atomic_json(status_file, status)
        children = set()
        worker = None
        awake = None
        reason = 'runner_exit'
        restart_count = 0
        try:
            if stop_reason(control):
                reason = stop_reason(control)
                return
            awake = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])
            while not stop_reason(control):
                worker = subprocess.Popen([sys.executable, __file__, 'child', str(control_path)],
                                          cwd=ROOT, start_new_session=True)
                status.update(status='running', runner_pid=worker.pid,
                              restart_count=restart_count)
                atomic_json(status_file, status)
                reason = None
                while worker.poll() is None:
                    children = process_tree(worker.pid)
                    reason = stop_reason(control)
                    if reason:
                        break
                    time.sleep(1)
                children |= process_tree(worker.pid)
                if reason:
                    # Cleanup must stop the active process tree before waiting;
                    # a worker may otherwise run indefinitely past the deadline.
                    break
                code = worker.wait()
                worker = None
                reason = stop_reason(control)
                if reason:
                    break
                restart_count += 1
                delay = min(300, 5 * (2 ** min(restart_count - 1, 6)))
                status.update(status='restarting', reason='runner_exit',
                              last_exit_code=code, restart_count=restart_count,
                              next_restart_at=time.time() + delay)
                atomic_json(status_file, status)
                with (Path(control['run_dir'])/'bounded.log').open('ab') as log:
                    log.write(('Runner exited early with code %s; restart %s in %ss.\n' %
                               (code, restart_count, delay)).encode())
                time.sleep(min(delay, max(0, control['deadline']-time.time())))
            reason = reason or stop_reason(control) or 'runner_exit'
        except (KeyboardInterrupt, SystemExit):
            reason = 'operator_stop'
        except Exception as error:
            reason = 'watchdog_error: ' + str(error)
            raise
        finally:
            if worker is not None:
                children |= process_tree(worker.pid)
                terminate(children)
                worker.wait()
            if awake is not None:
                awake.terminate()
                awake.wait()
            status.update(status='stopped', reason=reason, stopped_at=time.time(),
                          dataforseo_usd=spent(Path(control['run_dir'])/'dataforseo-spend.json'))
            atomic_json(status_file, status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('prepare')
    setup.add_argument('--repo', required=True)
    setup.add_argument('--id', required=True)
    setup.add_argument('--base', required=True,
                       help='Freshly fetched branch or commit to stack this run on')
    setup.add_argument('--budget-usd', type=float, default=0.084)
    setup.add_argument('--duration-seconds', type=int, required=True)
    setup.add_argument('--deadline', type=float, required=True)
    setup.add_argument('--deadline-local', required=True)
    for command in ('start', 'watch', 'child', 'guard'):
        sub.add_parser(command).add_argument('control')
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args)
    elif args.command == 'start':
        control_path = Path(args.control).resolve()
        control = read(control_path)
        with (Path(control['run_dir'])/'bounded.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, __file__, 'watch', str(control_path)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    cwd=ROOT, start_new_session=True)
        print(json.dumps({'watchdog_pid':proc.pid,'control':str(control_path)}))
    elif args.command == 'watch':
        watch(Path(args.control).resolve())
    elif args.command == 'guard':
        guard(Path(args.control).resolve())
    else:
        child(Path(args.control).resolve())


if __name__ == '__main__':
    main()
