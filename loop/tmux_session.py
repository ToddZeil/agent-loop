"""Experimental tmux lifecycle behind a durable, fail-closed worktree reservation.

Callers must hold the run's keeper lock and workspace lock throughout admission
and startup. A terminated tmux server does not prove that all descendants have
exited, so reconciliation requires explicit operator verification.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from .worker import reserve_terminal_workspace, terminal_owner_path


class TmuxSession:
    def __init__(self, home, workspace, record, executable=None):
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.record = record
        self.executable = executable or shutil.which('tmux')
        if not self.executable:
            raise RuntimeError('tmux is not installed')
        self.name = record['socket']

    def command(self, *args):
        return [self.executable, '-f', '/dev/null', '-L', self.name, *args]

    @classmethod
    def start(cls, home, workspace, argv, *, executable=None, env=None):
        """Reserve first, then launch. Any uncertain failure leaves the barrier."""
        binary = executable or shutil.which('tmux')
        if not binary:
            raise RuntimeError('tmux is not installed')
        if not argv or not all(isinstance(item, str) and item and '\0' not in item for item in argv):
            raise ValueError('A valid terminal command is required')
        environment = []
        for name, value in (env or {}).items():
            if not isinstance(name, str) or not name or '=' in name or '\0' in name or not isinstance(value, str) or '\0' in value:
                raise ValueError('Invalid terminal environment')
            environment.extend(['-e', name + '=' + value])
        owner_id = uuid.uuid4().hex
        record = {'version': 1, 'id': owner_id, 'socket': 'agent-loop-' + owner_id,
                  'workspace': str(Path(workspace).resolve()), 'home': str(Path(home).resolve())}
        reserve_terminal_workspace(home, workspace, record)
        session = cls(home, workspace, record, binary)
        # Do not use a shell: provider arguments are passed to tmux as argv.
        try:
            subprocess.run(session.command('new-session', '-d', '-s', 'agent', '-c',
                                           str(Path(workspace).resolve()), *environment, *argv), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            subprocess.run(session.command('set-option', '-t', 'agent', 'status', 'off'),
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('Could not start the tmux terminal; worktree remains reserved for recovery') from exc
        return session

    @classmethod
    def recover(cls, home, workspace, *, executable=None):
        """Read only records with exact identities and a matching workspace."""
        path = terminal_owner_path(home, workspace)
        try:
            record = json.loads(path.read_text())
            owner_id = record['id']
            if (record['version'] != 1 or not isinstance(owner_id, str) or len(owner_id) != 32
                    or any(c not in '0123456789abcdef' for c in owner_id)
                    or record['socket'] != 'agent-loop-' + owner_id
                    or record['workspace'] != str(Path(workspace).resolve())
                    or record['home'] != str(Path(home).resolve())):
                raise ValueError('Terminal ownership record is invalid')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError('Terminal ownership requires manual reconciliation') from exc
        return cls(home, workspace, record, executable)

    def alive(self):
        result = subprocess.run(self.command('has-session', '-t', '=agent'),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode == 0:
            return True
        error = result.stderr.strip()
        if ('No such file or directory' in error or error.startswith('no server running on ')
                or error.startswith("can't find session:")):
            return False
        raise RuntimeError('Cannot verify tmux session: ' + (error or 'unknown error'))

    def attach_command(self):
        if not self.alive():
            raise RuntimeError('Terminal session is missing; ownership requires reconciliation')
        return self.command('attach-session', '-t', '=agent')

    def stop(self):
        """Stop the session, retaining its reservation until safe reconciliation."""
        subprocess.run(self.command('kill-server'), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)

    def reconcile(self, *, verified_no_writers=False):
        """Clear a stopped reservation only after explicit operator verification.

        Callers must hold the keeper and both workspace locks. A missing tmux
        server alone is not evidence that detached descendants have exited.
        """
        if verified_no_writers is not True:
            raise ValueError('Verify that all terminal processes have exited before clearing ownership')
        current = self.recover(self.home, self.workspace, executable=self.executable)
        if current.record != self.record:
            raise RuntimeError('Terminal ownership changed; inspect it again')
        if current.alive():
            raise RuntimeError('Stop the tmux session before clearing ownership')
        path = terminal_owner_path(self.home, self.workspace)
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
