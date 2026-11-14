"""Snapshot a worker's changes, merge to main, validate and restart the local workspace."""
import argparse
import asyncio
import fcntl
from functools import lru_cache
import http.cookiejar
import json
import os
import re
import shlex
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

from .storage import atomic_json
from .worker import process_identity


def git(repo, *args, env=None):
    result = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


@lru_cache(maxsize=64)
def common_dir(repo):
    directory = Path(git(repo, 'rev-parse', '--git-common-dir'))
    return (directory if directory.is_absolute() else Path(repo) / directory).resolve()


def target_path(repo):
    return common_dir(repo).parent / '.agent-loop' / 'deploy' / 'target.json'


def read_target(repo):
    path = target_path(repo)
    if not path.exists():
        raise ValueError('Start the primary local workspace to register its merge-and-redeploy target.')
    target = json.loads(path.read_text())
    if common_dir(target['checkout']) != common_dir(repo):
        raise ValueError('The source worktree belongs to another repository')
    return target


def register(checkout, home, port):
    checkout = Path(checkout).resolve()
    # Independent worktree previews must never replace the primary deployment target.
    if common_dir(checkout).parent != checkout or Path(home).resolve() != checkout / '.agent-loop' / 'web':
        return
    path = target_path(checkout)
    previous = json.loads(path.read_text()) if path.exists() else {}
    browser_cache = next((p for p in (os.environ.get('PLAYWRIGHT_BROWSERS_PATH'), str(checkout / '.agent-loop' / 'playwright'), '/tmp/agent-loop-playwright') if p and Path(p).is_dir()), None)
    browser_check = (['env', 'PLAYWRIGHT_BROWSERS_PATH=' + browser_cache] if browser_cache else []) + ['npm', '--prefix', 'web', 'test']
    atomic_json(path, {**previous, 'checkout': str(checkout), 'home': str(Path(home).resolve()),
        'url': f'http://127.0.0.1:{port}', 'remote': previous.get('remote', 'origin'),
        'branch': previous.get('branch', 'main'), 'pid': os.getpid(), 'identity': process_identity(os.getpid()),
        'launch': [sys.executable, '-m', 'loop.web', '--home', str(Path(home).resolve()), '--port', str(port)],
        'checks': previous.get('checks', [[sys.executable, '-m', 'unittest', 'tests.test_native', 'tests.test_web', 'tests.test_continuous', 'tests.test_deploy'], ['node', '--check', 'web/app.js'], browser_check])})


def public_status(repo):
    try:
        target = read_target(repo)
    except (OSError, ValueError, RuntimeError):
        return None
    directory = target_path(repo).parent
    jobs = sorted((directory / 'jobs').glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    last = json.loads(jobs[0].read_text()) if jobs else None
    if last and last['stage'] not in ('complete', 'failed') and time.time() - last['updated_at'] > 10:
        pid = last.get('pid')
        if not pid or process_identity(pid) != last.get('identity'):
            last = json.loads(jobs[0].read_text())
            if last['stage'] not in ('complete', 'failed'):
                last = {**last, 'stage': 'failed', 'error': 'Deployment process ended before completion; inspect its log and retry.'}
    return {'checkout': target['checkout'], 'branch': target['branch'], 'url': target['url'], 'job': last}


def failure_message(path, error):
    if not isinstance(error, subprocess.CalledProcessError):
        return str(error)
    log=Path(path).with_suffix('.log')
    if log.exists():
        with log.open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-65536))
            text=stream.read().decode(errors='replace')
        text=re.sub(r'\x1b\[[0-9;]*m','',text)
        names=re.findall(r'^\s+\d+\) (.+)$',text,re.MULTILINE)
        if names:
            return 'Browser tests failed: '+('; '.join(name.split(' › ',1)[-1] for name in names))[:500]
        names=re.findall(r'^(?:FAIL|ERROR): (.+)$',text,re.MULTILINE)
        if names:
            return 'Validation failed: '+('; '.join(names))[:500]
    return f'Validation command failed (exit {error.returncode}). Open the deployment log for details.'


def snapshot(source, ident, directory):
    """Use a private Git index: never stage, commit or switch the worker's branch."""
    if git(source, 'ls-files', '--unmerged'):
        raise ValueError('Resolve the source worktree merge conflicts first')
    head = git(source, 'rev-parse', 'HEAD')
    with tempfile.TemporaryDirectory(dir=directory) as temp:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git(source, 'read-tree', head, env=env)
        git(source, 'add', '-A', '--', '.', env=env)
        tree = git(source, 'write-tree', env=env)
        # Compare the captured tree with the live worktree; refuse a torn snapshot.
        if git(source, 'diff', '--no-ext-diff', '--name-only', env=env):
            raise ValueError('Files changed while capturing the deployment; retry after the edits finish')
        if git(source, 'ls-files', '--others', '--exclude-standard', env=env):
            raise ValueError('New files appeared while capturing the deployment; retry')
        if git(source, 'rev-parse', 'HEAD') != head:
            raise ValueError('The source branch changed while capturing the deployment; retry')
        commit = head if tree == git(source, 'rev-parse', head + '^{tree}') else git(
            source, '-c', 'user.name=Local workspace', '-c', 'user.email=workspace@localhost',
            'commit-tree', tree, '-p', head, '-m', 'Deploy worker changes ' + ident)
    git(source, 'update-ref', 'refs/agent-loop/deploy/' + ident, commit)
    return commit


def launch_job(path, root):
    with path.with_suffix('.log').open('ab') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'loop.deploy', 'run', str(path)], cwd=root,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


def submit(source, message=''):
    source = Path(git(Path(source).resolve(), 'rev-parse', '--show-toplevel')).resolve()
    target = read_target(source)
    root = Path(target['checkout'])
    if git(root, 'status', '--porcelain'):
        raise ValueError('The serving checkout has uncommitted changes; commit them before deploying')
    ident = uuid.uuid4().hex[:16]
    directory = target_path(source).parent
    directory.mkdir(parents=True, exist_ok=True)
    commit = snapshot(source, ident, directory)
    jobs = directory / 'jobs'; jobs.mkdir(exist_ok=True)
    path = jobs / (ident + '.json')
    atomic_json(path, {'id': ident, 'source': str(source), 'source_commit': commit,
        'message': message[:1000], 'stage': 'queued', 'created_at': time.time(), 'updated_at': time.time()})
    try:
        pid = launch_job(path, root)
    except OSError as exc:
        job = json.loads(path.read_text()); job.update(stage='failed', error=str(exc), updated_at=time.time()); atomic_json(path, job)
        raise
    return {'id': ident, 'pid': pid, 'status_file': str(path), 'url': target['url']}


class Host:
    def __init__(self, target):
        self.target = target
        self.url = target['url']
        self.stopped = False
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.client.open(self.url, timeout=3).read()

    def call(self, path, body=None):
        request = urllib.request.Request(self.url + '/api' + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Origin': self.url, 'Content-Type': 'application/json', 'X-Loop-Request': '1'})
        return json.load(self.client.open(request, timeout=10))

    def state(self):
        return self.call('/supervisor')['supervisor']

    def capture(self):
        state = self.state()
        active = []
        for task in state['tasks']:
            if not task.get('terminal_open'):
                continue
            if task.get('terminal_mode') == 'shell' or not task.get('native_session_id'):
                raise ValueError('Close worktree shells and save chat IDs before redeploying')
            active.append({'id': task['id'], 'session_id': task['native_session_id']})
        return {'running': state['running'], 'active': active}

    def stop(self):
        self.call('/supervisor/stop', {})
        deadline = time.monotonic() + 90
        while self.state()['running']:
            if time.monotonic() > deadline:
                raise RuntimeError('Supervisor is still finishing its current operation; deployment was not forced')
            time.sleep(.2)
        pid = self.target['pid']
        if process_identity(pid) != self.target['identity']:
            raise RuntimeError('The local server process changed; refusing to stop a different process')
        os.kill(pid, signal.SIGTERM)
        self.stopped = True
        deadline = time.monotonic() + 15
        while process_identity(pid):
            if time.monotonic() > deadline:
                raise RuntimeError('The previous local server did not exit')
            time.sleep(.1)

    @staticmethod
    def start(target):
        log_path = Path(target['home']) / 'server.log'
        with log_path.open('ab') as log:
            proc = subprocess.Popen(target['launch'], cwd=target['checkout'], stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('The new server exited; see ' + str(log_path))
            try:
                return Host(target)
            except (OSError, ValueError):
                time.sleep(.2)
        raise RuntimeError('The new local server did not become healthy')

    async def restore(self, saved):
        from aiohttp import ClientSession, CookieJar, ClientResponseError
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            async with client.get(self.url) as response:
                await response.read()
            for old in saved['active']:
                state = self.state()
                task = next(t for t in state['tasks'] if t['id'] == old['id'])
                if task['native_session_id'] != old['session_id']:
                    raise RuntimeError('Saved chat ID changed for ' + old['id'])
                if task['terminal_open']:
                    continue
                endpoint = f"{self.url}/api/fleets/{state['id']}/tasks/{old['id']}/terminal?mode=agent"
                try:
                    socket = await client.ws_connect(endpoint, headers={'Origin': self.url})
                except ClientResponseError:
                    if not next(t for t in self.state()['tasks'] if t['id'] == old['id'])['terminal_open']:
                        raise
                else:
                    await asyncio.sleep(.3)
                    await socket.close()
            if saved['running'] and not self.state()['running']:
                self.call('/supervisor/resume', {})


def execute(path, *, host_factory=Host):
    path = Path(path)
    job = json.loads(path.read_text())
    job.update(pid=os.getpid(), identity=process_identity(os.getpid()))
    atomic_json(path, job)
    target = read_target(job['source'])
    root = Path(target['checkout'])
    directory = path.parent.parent
    worktree = directory / 'worktrees' / job['id']
    worktree.parent.mkdir(parents=True, exist_ok=True)
    stopped, saved, host = False, None, None
    def stage(name, **values):
        job.update(stage=name, updated_at=time.time(), **values); atomic_json(path, job)
    lock = (directory / 'deploy.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        # A queued deployment uses the current server PID after earlier deployments finish.
        target = read_target(root)
        host = host_factory(target)
        saved = host.capture()
        stage('preparing')
        if git(root, 'status', '--porcelain'):
            raise ValueError('The serving checkout has uncommitted changes')
        original = git(root, 'rev-parse', 'HEAD')
        git(root, 'fetch', target['remote'], target['branch'])
        base = git(root, 'rev-parse', 'FETCH_HEAD')
        git(root, 'worktree', 'add', '--detach', str(worktree), base)
        # Include the currently running release as well as the worker's snapshot.
        for commit in (original, job['source_commit']):
            git(worktree, '-c', 'user.name=Local workspace', '-c', 'user.email=workspace@localhost',
                'merge', '--no-edit', commit)
        runtime = root / '.agent-loop' / 'web-venv'
        if runtime.is_dir():
            (worktree / '.agent-loop').mkdir(exist_ok=True)
            (worktree / '.agent-loop' / 'web-venv').symlink_to(runtime, target_is_directory=True)
        dependencies = root / 'web' / 'node_modules'
        if dependencies.is_dir():
            changed = git(worktree, 'diff', original, '--name-only', '--', 'web/package.json', 'web/package-lock.json')
            if changed:
                subprocess.run(['npm', 'ci', '--prefix', 'web'], cwd=worktree, check=True, timeout=300)
            else:
                shutil.copytree(dependencies, worktree / 'web' / 'node_modules', symlinks=True)
        stage('checking')
        for command in target['checks']:
            print('Checking:', command, flush=True)
            subprocess.run(command, cwd=worktree, check=True, timeout=900)
        if git(worktree, 'diff', 'HEAD', '--name-only'):
            raise RuntimeError('Validation modified tracked files; refusing to publish the changed candidate')
        if git(root, 'rev-parse', 'HEAD') != original or git(root, 'status', '--porcelain'):
            raise RuntimeError('The serving checkout changed during validation; retry deployment')
        # Recheck live terminal recovery before the irreversible publication.
        host.capture()
        commit = git(worktree, 'rev-parse', 'HEAD')
        stage('publishing', commit=commit)
        git(root, 'push', target['remote'], commit + ':refs/heads/' + target['branch'])
        stage('restarting', main_published=True)
        saved = host.capture()
        job['recovery'] = saved; atomic_json(path, job)
        host.stop(); stopped = True
        git(root, 'merge', '--ff-only', commit)
        if dependencies.is_dir() and changed:
            old_dependencies = directory / ('dependencies-' + job['id'])
            os.replace(dependencies, old_dependencies)
            try:
                os.replace(worktree / 'web' / 'node_modules', dependencies)
            except OSError:
                os.replace(old_dependencies, dependencies)
                raise
            shutil.rmtree(old_dependencies, ignore_errors=True)
        host = host_factory.start(target)
        stopped = False
        stage('restoring')
        asyncio.run(host.restore(saved))
        if host.state().get('server_commit') != commit:
            raise RuntimeError('The running server did not report the deployed commit')
        stage('complete', error=None)
    except Exception as exc:
        detail=failure_message(path,exc)
        if isinstance(exc,subprocess.CalledProcessError):
            job['failed_check']=shlex.join(exc.cmd)
        if stopped or getattr(host, 'stopped', False):
            try:
                host = host_factory.start(target)
                asyncio.run(host.restore(saved))
            except Exception as recovery_error:
                stage('failed', error=detail, recovery_error=str(recovery_error)); return False
        # A stopped scheduler is restored even if publication/restart failed beforehand.
        elif saved and saved['running'] and host:
            try:
                if not host.state()['running']:
                    host.call('/supervisor/resume', {})
            except Exception:
                pass
        stage('failed', error=detail)
        return False
    finally:
        if worktree.exists():
            try:
                git(root, 'worktree', 'remove', '--force', str(worktree))
            except RuntimeError:
                pass
        lock.close()
    return True


def main():
    parser = argparse.ArgumentParser(description='Merge a worker snapshot to main and redeploy the local workspace')
    parser.add_argument('--source', default=os.getcwd())
    parser.add_argument('--message', default='')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('operation', nargs='?', choices=['run'])
    parser.add_argument('job', nargs='?')
    args = parser.parse_args()
    try:
        if args.operation == 'run':
            raise SystemExit(0 if execute(args.job) else 1)
        print(json.dumps(public_status(args.source) if args.status else submit(args.source, args.message), indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
