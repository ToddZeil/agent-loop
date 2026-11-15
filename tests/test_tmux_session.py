"""Real tmux lifecycle tests using disposable sessions and lock records."""
import fcntl
import json
import shutil
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from loop.tmux_session import TmuxSession
from loop.continuous import worktree_admitted
from loop.worker import terminal_owner_path, workspace_lock_path, require_unreserved_workspace


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class TmuxSessionTests(unittest.TestCase):
    def test_keeper_operation_holds_admission_through_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered, release = threading.Event(), threading.Event()
            class Keeper:
                dir = root / 'one'
                worktree = root
                @worktree_admitted
                def operation(self):
                    entered.set()
                    self.assert_release = release.wait(5)
            keeper = Keeper()
            thread = threading.Thread(target=keeper.operation)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                path = workspace_lock_path(root / 'other-home', root)
                with path.open('a') as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(keeper.assert_release)

    def test_missing_worktree_is_not_created_by_lock_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'missing'
            path = workspace_lock_path(root / 'home', workspace)
            path.parent.mkdir(parents=True)
            self.assertFalse(workspace.exists())

    def test_different_homes_share_the_same_worktree_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            subprocess.run(['git', '-C', str(workspace), 'init', '-q'], check=True)
            one, two = root / 'home-one', root / 'home-two'
            first = workspace_lock_path(one, workspace)
            self.assertEqual(first, workspace_lock_path(two, workspace))
            first.parent.mkdir(parents=True)
            with first.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with workspace_lock_path(two, workspace).open('a') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                session = TmuxSession.start(one, workspace, ['/bin/sh', '-i'])
                try:
                    with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                        require_unreserved_workspace(two, workspace)
                    with self.assertRaisesRegex(RuntimeError, 'manual reconciliation'):
                        TmuxSession.recover(two, workspace)
                finally:
                    session.stop()

    def test_recover_after_controller_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = '''
import fcntl, os, signal, sys
from pathlib import Path
from loop.tmux_session import TmuxSession
from loop.worker import workspace_lock_path
root = Path(sys.argv[1])
path = workspace_lock_path(root, root)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    TmuxSession.start(root, root, ['/bin/sh', '-i'])
    os.kill(os.getpid(), signal.SIGKILL)
'''
            process = subprocess.run([sys.executable, '-c', code, str(root)])
            self.assertEqual(process.returncode, -9)
            session = TmuxSession.recover(root, root)
            try:
                self.assertTrue(session.alive())
                with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                    require_unreserved_workspace(root, root)
            finally:
                session.stop()

    def test_reservation_survives_recovery_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = workspace_lock_path(root, root)
            lock_path.parent.mkdir(parents=True)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                session = TmuxSession.start(root, root, ['/bin/sh', '-i'])
                try:
                    self.assertTrue(session.alive())
                    with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                        require_unreserved_workspace(root, root)
                    recovered = TmuxSession.recover(root, root)
                    self.assertEqual(recovered.name, session.name)
                    self.assertTrue(recovered.alive())
                    self.assertIn('attach-session', recovered.attach_command())
                    with self.assertRaisesRegex(RuntimeError, 'Stop the tmux'):
                        recovered.reconcile(verified_no_writers=True)
                    recovered.stop()
                    self.assertFalse(recovered.alive())
                    self.assertTrue(terminal_owner_path(root, root).exists())
                    with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                        recovered.attach_command()
                    with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                        require_unreserved_workspace(root, root)
                    with self.assertRaisesRegex(ValueError, 'Verify'):
                        recovered.reconcile()
                    with mock.patch('loop.tmux_session.subprocess.run',
                                    return_value=subprocess.CompletedProcess([], 1, '', 'permission denied')):
                        with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
                            recovered.reconcile(verified_no_writers=True)
                    self.assertTrue(terminal_owner_path(root, root).exists())
                    recovered.reconcile(verified_no_writers=True)
                    require_unreserved_workspace(root, root)
                finally:
                    session.stop()

    def test_corrupt_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = terminal_owner_path(root, root)
            marker.parent.mkdir(parents=True)
            marker.write_text('{incomplete')
            with self.assertRaisesRegex(RuntimeError, 'manual reconciliation'):
                TmuxSession.recover(root, root)
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                require_unreserved_workspace(root, root)

    def test_reconcile_rejects_replaced_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = workspace_lock_path(root, root)
            lock_path.parent.mkdir(parents=True)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                session = TmuxSession.start(root, root, ['/bin/sh', '-i'])
                try:
                    session.stop()
                    marker = terminal_owner_path(root, root)
                    replacement = {**session.record, 'id': '0' * 32, 'socket': 'agent-loop-' + '0' * 32}
                    marker.write_text(json.dumps(replacement))
                    with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                        session.reconcile(verified_no_writers=True)
                    self.assertTrue(marker.exists())
                finally:
                    session.stop()

    def test_failed_launch_keeps_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = workspace_lock_path(root, root)
            lock_path.parent.mkdir(parents=True)
            with lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(FileNotFoundError):
                    TmuxSession.start(root, root, ['/bin/sh'], executable='/not/a/tmux')
                self.assertTrue(terminal_owner_path(root, root).exists())
                with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                    require_unreserved_workspace(root, root)


if __name__ == '__main__':
    unittest.main()
