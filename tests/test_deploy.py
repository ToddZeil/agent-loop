"""Real Git integration and publication failure tests; no remote service or model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from loop.deploy import git, submit, execute, target_path, public_status
from loop.storage import atomic_json


class DeployTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = self.directory / 'app'; self.root.mkdir()
        git(self.root, 'init', '-q')
        git(self.root, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(self.root, 'config', 'user.name', 'Test')
        git(self.root, 'config', 'user.email', 'test@localhost')
        (self.root / '.gitignore').write_text('.agent-loop/\n.env\n__pycache__/\n')
        (self.root / 'app.py').write_text('value = 1\n')
        git(self.root, 'add', '.'); git(self.root, 'commit', '-qm', 'base')
        self.base = git(self.root, 'rev-parse', 'HEAD')
        self.remote = self.directory / 'remote.git'
        git(self.root, 'init', '--bare', '-q', str(self.remote))
        git(self.root, 'remote', 'add', 'origin', str(self.remote))
        git(self.root, 'push', '-u', 'origin', 'main')
        self.source = self.directory / 'worker'
        git(self.root, 'worktree', 'add', '-qb', 'worker', str(self.source))
        (self.source / 'app.py').write_text('value = 2\n')
        git(self.source, 'add', 'app.py')
        (self.source / 'app.py').write_text('value = 3\n')
        (self.source / 'new.txt').write_text('new worker file\n')
        (self.source / '.env').write_text('ignored secret\n')
        self.index = git(self.source, 'ls-files', '--stage')
        self.original_status = git(self.source, 'status', '--porcelain')
        self.config = {'checkout': str(self.root), 'home': str(self.root / '.agent-loop/web'),
            'url': 'http://127.0.0.1:9999', 'remote': 'origin', 'branch': 'main',
            'checks': [[sys.executable, '-c', 'from app import value; assert value == 3']]}
        atomic_json(target_path(self.root), self.config)
        root = self.root
        class FakeHost:
            events = []
            running = True
            def __init__(self, target): self.target = target
            def capture(self): return {'running': True, 'active': [{'id': 'worker', 'session_id': 'exact-chat'}]}
            def stop(self): self.events.append('stop'); type(self).running = False
            @classmethod
            def start(cls, target): cls.events.append('start'); return cls(target)
            async def restore(self, saved):
                self.events.append(('restore', saved)); type(self).running = saved['running']
            def state(self): return {'running': self.running, 'server_commit': git(root, 'rev-parse', 'HEAD')}
            def call(self, path, body): type(self).running = True
        self.host = FakeHost

    def queue(self):
        with mock.patch('loop.deploy.launch_job', return_value=999):
            job = submit(self.source)
        return Path(job['status_file'])

    def unchanged_worker(self):
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(git(self.source, 'status', '--porcelain'), self.original_status)
        self.assertEqual(git(self.source, 'ls-files', '--stage'), self.index)

    def test_snapshot_publishes_and_redeploys_without_changing_worker_branch_or_index(self):
        path = self.queue()
        self.unchanged_worker()
        self.assertTrue(execute(path, host_factory=self.host))
        result = json.loads(path.read_text())
        self.assertEqual(result['stage'], 'complete')
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual((self.root / 'app.py').read_text(), 'value = 3\n')
        self.assertTrue((self.root / 'new.txt').is_file())
        self.assertNotIn('.env', git(self.root, 'ls-tree', '--name-only', 'HEAD').splitlines())
        self.assertEqual(self.host.events[:2], ['stop', 'start'])
        self.assertEqual(self.host.events[2][1]['active'][0]['session_id'], 'exact-chat')
        self.unchanged_worker()
        self.assertEqual(public_status(self.root)['job']['id'], result['id'])

    def test_failed_validation_never_publishes_or_stops_the_server(self):
        self.config['checks'] = [[sys.executable, '-c', 'raise SystemExit(7)']]
        atomic_json(target_path(self.root), self.config)
        path = self.queue()
        self.assertFalse(execute(path, host_factory=self.host))
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), self.base)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.host.events, [])
        self.assertEqual(json.loads(path.read_text())['stage'], 'failed')
        self.unchanged_worker()

    def test_merge_conflicts_preserve_main_source_and_running_workers(self):
        (self.root / 'app.py').write_text('value = 4\n')
        git(self.root, 'add', '.'); git(self.root, 'commit', '-qm', 'concurrent main edit')
        git(self.root, 'push', 'origin', 'main')
        main = git(self.root, 'rev-parse', 'HEAD')
        path = self.queue()
        self.assertFalse(execute(path, host_factory=self.host))
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), main)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), main)
        self.assertEqual(self.host.events, [])
        self.unchanged_worker()
        self.assertIn('CONFLICT', json.loads(path.read_text())['error'])

    def test_dirty_serving_checkout_is_not_overwritten(self):
        (self.root / 'operator.txt').write_text('keep me')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            self.queue()
        self.assertEqual((self.root / 'operator.txt').read_text(), 'keep me')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.base)

    def test_manual_checkout_edit_during_checks_is_preserved(self):
        self.config['checks'] = [[sys.executable, '-c', 'from pathlib import Path; Path('+repr(str(self.root / 'operator.txt'))+').write_text("keep me")']]
        atomic_json(target_path(self.root), self.config)
        path = self.queue()
        self.assertFalse(execute(path, host_factory=self.host))
        self.assertEqual((self.root / 'operator.txt').read_text(), 'keep me')
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), self.base)
        self.assertEqual(self.host.events, [])

    def test_remote_advance_is_rejected_without_force_push_or_restart(self):
        # An independent descendant of main races the checked candidate publication.
        other = git(self.root, 'commit-tree', self.base+'^{tree}', '-p', self.base, '-m', 'remote race')
        git(self.root, 'push', 'origin', other+':refs/heads/race')
        path = self.queue()
        actual_git = git
        def racing_git(repo, *args, **kwargs):
            if args[0] == 'push': actual_git(self.remote, 'update-ref', 'refs/heads/main', other)
            return actual_git(repo, *args, **kwargs)
        with mock.patch('loop.deploy.git', side_effect=racing_git):
            self.assertFalse(execute(path, host_factory=self.host))
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), other)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(self.host.events, [])

    def test_interrupted_job_does_not_leave_deploy_button_blocked(self):
        import time
        path=self.queue()
        job=json.loads(path.read_text());job.update(updated_at=time.time()-30,pid=99999999,identity='gone')
        atomic_json(path,job)
        self.assertEqual(public_status(self.root)['job']['stage'],'failed')
        self.assertIn('process ended',public_status(self.root)['job']['error'])

    def test_command_from_subdirectory_captures_the_entire_worktree(self):
        subdirectory=self.source/'subdirectory';subdirectory.mkdir()
        with mock.patch('loop.deploy.launch_job',return_value=999):
            result=submit(subdirectory)
        job=json.loads(Path(result['status_file']).read_text())
        self.assertEqual(job['source'],str(self.source.resolve()))
        self.assertEqual(git(self.source,'show',job['source_commit']+':app.py'),'value = 3')
        self.assertIn('new.txt',git(self.source,'ls-tree','--name-only',job['source_commit']))
        self.unchanged_worker()

    def test_failed_browser_check_reports_test_names_and_retains_command_in_log(self):
        from loop.deploy import failure_message
        path=self.queue()
        path.with_suffix('.log').write_text('  1) tests/workspace.spec.cjs:129:1 › delete and clean up agents \n  2) tests/workspace.spec.cjs:182:1 › token totals \n')
        error=subprocess.CalledProcessError(1,['npm','--prefix','web','test'])
        summary=failure_message(path,error)
        self.assertIn('delete and clean up agents',summary)
        self.assertIn('token totals',summary)
        self.assertNotIn("Command '[",summary)
