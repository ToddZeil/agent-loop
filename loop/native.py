"""Interactive CLI launch and exact-session recovery, without shell evaluation."""
import hashlib
import json
import os
import shlex
import uuid
from datetime import datetime
from pathlib import Path

PROVIDERS = ('codex', 'claude', 'antigravity', 'muse')


def terminal_config(run, body=None):
    current = run.load('native-config.json', {}) or {}
    if body is None or not body:
        return current or {'provider': run.state()['provider']}
    provider = body.get('program', current.get('provider', run.state()['provider']))
    if provider == 'agent':
        provider = run.state()['provider']
    if provider not in (*PROVIDERS, 'custom'):
        raise ValueError('Choose a supported CLI or custom command')
    result = {'provider': provider}
    if provider == 'custom':
        for name in ('command', 'resume_command'):
            value = body.get(name, current.get(name, []))
            if isinstance(value, str):
                value = shlex.split(value)
            if not isinstance(value, list) or any(not isinstance(v, str) or '\x00' in v for v in value):
                raise ValueError('Terminal commands must contain executable arguments')
            if sum(map(len, value)) > 16000:
                raise ValueError('Terminal command is too long')
            result[name] = value
        if not result['command']:
            raise ValueError('Enter the CLI executable and arguments')
        if result['resume_command'] and not any('{session_id}' in arg for arg in result['resume_command']):
            raise ValueError('The resume command must contain {session_id}')
    return result


def session_key(provider, config):
    if provider != 'custom':
        return provider
    return 'custom:' + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def saved_session(run, provider, config=None):
    config = config or {'provider': provider}
    saved = (run.load('native-sessions.json', {}) or {}).get(session_key(provider, config), {})
    if not saved:
        legacy = run.load('native-session.json', {}) or {}
        if legacy.get('provider', 'codex') == provider and (provider != 'custom' or legacy.get('config') == config):
            saved = legacy
    return saved


def store_session(run, provider, session, config, *, manual=False, fresh=False):
    import time
    record = {'provider': provider, 'session_id': session, 'worktree': str(run.worktree),
              'saved_at': time.time(), 'manual': manual, 'fresh': fresh, 'config': config}
    sessions = run.load('native-sessions.json', {}) or {}
    sessions[session_key(provider, config)] = record
    run.save('native-sessions.json', sessions)
    run.save('native-session.json', record)
    return record


def latest_session(run, provider='codex', config=None):
    state = run.state()
    saved = saved_session(run, provider, config)
    if saved.get('manual'):
        return saved.get('session_id')
    active = state.get('interactive_session', {})
    if active.get('provider', 'codex') == provider and active.get('session_id'):
        return active['session_id']
    if state['provider'] == provider and state.get('session_id'):
        return state['session_id']
    paths = sorted(run.path('results').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            envelope = json.loads(path.read_text())
            record = run.load('attempts/' + str(envelope.get('attempt_id', path.stem)) + '.json', {})
            if record.get('task', {}).get('provider') != provider:
                continue
            result = envelope.get('result', {})
            if result.get('session_id'):
                if saved.get('session_id') and saved.get('saved_at', 0) > envelope.get('finished_at', path.stat().st_mtime):
                    return saved['session_id']
                return result['session_id']
        except (OSError, ValueError, AttributeError):
            continue
    return saved.get('session_id')


def discover_session(worktree, opened_at, exclude=()):
    """Read only rollout headers matching this worktree and terminal creation time."""
    root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'
    target = Path(worktree).resolve()
    candidates = []
    for path in root.glob('*/*/*/*.jsonl'):
        try:
            if path.stat().st_mtime < opened_at - 2:
                continue
            with path.open() as stream:
                row = json.loads(stream.readline(262144))
            data = row.get('payload', {})
            if row.get('type') != 'session_meta' or data.get('source') != 'cli':
                continue
            if Path(data.get('cwd', '')).resolve() != target:
                continue
            created = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00')).timestamp()
            if created >= opened_at - 2 and isinstance(data.get('id'), str) and data['id'] not in exclude:
                candidates.append((created, data['id']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Ambiguity is safer than reopening a different concurrent CLI chat.
    ids = {session for _, session in candidates}
    return ids.pop() if len(ids) == 1 else None


def activity_hooks(path):
    # Claude reports its own turn state; the dashboard reads it to tell working from idle.
    def write(state):
        return {'type': 'command', 'command': "printf '{\"state\":\"%s\",\"at\":%%s}\\n' \"$(date +%%s)\" > %s" % (state, shlex.quote(str(path)))}
    def on(state, matcher=None):
        entry = {'hooks': [write(state)]}
        if matcher:
            entry['matcher'] = matcher
        return entry
    return {'hooks': {
        'SessionStart': [on('idle')],
        'UserPromptSubmit': [on('working')],
        'PreToolUse': [on('working')],
        'PostToolUse': [on('working')],
        'Stop': [on('idle')],
        'Notification': [on('needs_you', 'permission_prompt|elicitation_dialog'), on('idle', 'idle_prompt')],
    }}


def agent_terminal_command(run, config=None):
    config = config or terminal_config(run)
    provider = config['provider']
    state = run.state()
    session = latest_session(run, provider, config)
    model = state.get('model') if provider == state['provider'] else None
    full_access = run.config.get('full_access', True)
    effort = run.config.get('worker_effort') or 'medium'
    instructions = ('Work in this managed worktree. You may edit all project files. '
                    + ('You may commit, rebase, push and switch branches when the objective needs it; '
                       'the supervisor adopts whatever HEAD you leave. ' if run.config.get('allow_git') and full_access else
                       'Do not manually commit, push, or change branches; the supervisor manages Git. ') +
                    'Use the managed deployment helper, when provided, for user-requested publishing.\n' + run._prompt(state))
    if provider == 'codex':
        options = ['--no-daemon', '--no-alt-screen', '--cd', str(run.worktree), '--sandbox', 'danger-full-access' if full_access else 'workspace-write',
                   '--ask-for-approval', 'never' if full_access else 'on-request', '-c', 'developer_instructions=' + json.dumps(instructions),
                   '-c', 'model_reasoning_effort=' + json.dumps(effort)]
        if model:
            options += ['--model', model]
        flag = '--enable' if run.config.get('allow_subagents', True) else '--disable'
        options += [flag, 'multi_agent', flag, 'multi_agent_v2']
        argv = ['codex', 'resume', *options, session] if session else ['codex', *options]
    elif provider == 'claude':
        old = saved_session(run, provider, config)
        home = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        has_history = bool(session and next((home / 'projects').glob('*/' + session + '.jsonl'), None))
        fresh = not session or (old.get('fresh') and not has_history)
        session = session or str(uuid.uuid4())
        argv = ['claude', '--session-id' if fresh else '--resume', session,
                '--append-system-prompt', instructions, '--effort', effort]
        if full_access:
            argv += ['--dangerously-skip-permissions']
        if model:
            argv += ['--model', model]
        if not run.config.get('allow_subagents', True):
            argv += ['--disallowedTools', 'Agent']
        argv += ['--settings', json.dumps(activity_hooks(run.dir / 'activity.json'))]
    elif provider == 'antigravity':
        argv = ['agy', '--add-dir', str(run.worktree)]
        if full_access:
            argv += ['--dangerously-skip-permissions']
        if model:
            argv += ['--model', model]
        if str(model or '').rsplit('-', 1)[-1] not in ('low', 'medium', 'high'):
            argv += ['--effort', effort]
        if session:
            argv += ['--conversation', session]
    elif provider == 'muse':
        argv = ['muse', '--workspace', str(run.worktree), '--approval-mode', 'never' if full_access else 'on-request', '--reasoning-effort', effort]
        if full_access:
            argv += ['--disable-sandbox', '--disable-approval', '--trust-workspace']
        if model:
            argv += ['--model', model]
        if session:
            argv += ['resume', session]
    elif provider == 'custom':
        template = config['resume_command'] if session and config.get('resume_command') else config['command']
        if session and not config.get('resume_command') and not any('{session_id}' in arg for arg in template):
            raise ValueError('Set a resume command containing {session_id} to restore this custom chat')
        if not session and any('{session_id}' in arg for arg in template):
            raise ValueError('Set the exact session ID required by this command')
        argv = [arg.replace('{worktree}', str(run.worktree)).replace('{session_id}', session or '') for arg in template]
        argv[0] = os.path.expanduser(argv[0])
    else:
        raise ValueError('Unsupported interactive CLI')
    return argv, session, provider


def codex_terminal_command(run):
    if run.state()['provider'] != 'codex':
        raise ValueError('The interactive Codex terminal requires a Codex worker')
    argv, session, _ = agent_terminal_command(run, {'provider': 'codex'})
    return argv, session
