#!/usr/bin/env python3
"""Test transferring worktree locks into a disposable tmux pane on POSIX."""
import array
import fcntl
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid


def pane(socket_path, resistant=False):
    # The pane receives the *same open file descriptions* that were locked by
    # the controller. Keeping these descriptors open retains both flock locks.
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(socket_path)
        _, ancillary, _, _ = channel.recvmsg(1, socket.CMSG_SPACE(2 * array.array('i').itemsize))
        fds = array.array('i')
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
        if len(fds) != 2:
            raise RuntimeError('Expected both lock descriptors')
        # Launch only after both locks have arrived; retain copies in this wrapper
        # as well as the child. A production wrapper needs robust signal handling.
        command = ['/bin/sh', '-i']
        if resistant:
            command = [sys.executable, '-c',
                       'import os,signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); '
                       '[os.close(int(fd)) for fd in os.environ["PROBE_FDS"].split(",")]; time.sleep(60)']
        child = subprocess.Popen(command, pass_fds=tuple(fds),
                                 env={**os.environ, 'PROBE_FDS': ','.join(map(str, fds))})
        channel.sendall(str(child.pid).encode())
        child.wait()


def guardian(socket_path):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect(socket_path)
        _, ancillary, _, _ = channel.recvmsg(1, socket.CMSG_SPACE(2 * array.array('i').itemsize))
        fds = array.array('i')
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
        if len(fds) != 2:
            raise RuntimeError('Expected both lock descriptors')
        channel.sendall(b'R')
        while True:
            time.sleep(60)


def locked(path):
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def wait_for(predicate, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise RuntimeError('Timed out: ' + message)


def main():
    tmux = shutil.which('tmux')
    if not tmux:
        raise SystemExit('Install tmux to run this isolated probe.')
    name = 'agent-loop-lock-probe-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='tmux-lock-probe-') as directory:
        root = Path(directory)
        paths = [root / 'keeper.lock', root / 'workspace.lock']
        handles = [path.open('a') for path in paths]
        server = socket.socket(socket.AF_UNIX)
        dual_holder = None
        try:
            for handle in handles:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            address = str(root / 'transfer.sock')
            server.bind(address)
            os.chmod(address, 0o600)
            server.listen(1)
            server.settimeout(8)
            command = [tmux, '-L', name]
            if '--guardian-only' in sys.argv:
                subprocess.run([*command, 'new-session', '-d', '-s', 'probe', '/bin/sh', '-i'], check=True)
                holder = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--guardian', address])
                try:
                    with server.accept()[0] as conn:
                        descriptors = array.array('i', [h.fileno() for h in handles])
                        conn.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                        assert conn.recv(1) == b'R'
                    for handle in handles:
                        handle.close()
                    assert all(locked(path) for path in paths)
                    holder.kill()
                    holder.wait()
                    assert not any(locked(path) for path in paths)
                    subprocess.run([*command, 'has-session', '-t', 'probe'], check=True)
                    print('OBSERVED: guardian crash released both locks while the tmux shell remained alive')
                finally:
                    if holder.poll() is None:
                        holder.kill()
                        holder.wait()
                return
            if '--dual' in sys.argv:
                dual_holder = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--guardian', address])
                with server.accept()[0] as conn:
                    descriptors = array.array('i', [h.fileno() for h in handles])
                    conn.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                    assert conn.recv(1) == b'R'
            subprocess.run([*command, 'new-session', '-d', '-s', 'probe',
                            sys.executable, str(Path(__file__).resolve()), '--pane', address,
                            *(['--resistant'] if '--resistant' in sys.argv else [])], check=True)
            with server.accept()[0] as conn:
                descriptors = array.array('i', [h.fileno() for h in handles])
                conn.sendmsg([b'L'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                child_pid = int(conn.recv(32))
            # Emulate the web controller disappearing after acknowledged handoff.
            for handle in handles:
                handle.close()
            assert all(locked(path) for path in paths), 'A lock was lost when the controller exited'
            pane_pid = int(subprocess.check_output([*command, 'display-message', '-p', '-t', 'probe', '#{pane_pid}']))
            if '--resistant' not in sys.argv:
                subprocess.run([*command, 'send-keys', '-t', 'probe', 'echo OWNED_TMUX_READY', 'Enter'], check=True)
                def captured():
                    return b'OWNED_TMUX_READY' in subprocess.check_output([*command, 'capture-pane', '-p', '-t', 'probe'])
                wait_for(captured, 'shell input')
            else:
                time.sleep(.3)
            assert all(locked(path) for path in paths), 'The live pane lost its locks'
            print(f'PASS: locks retained after controller handoff; pane {pane_pid}, child {child_pid}')
            if dual_holder:
                dual_holder.kill()
                dual_holder.wait()
                assert all(locked(path) for path in paths), 'Pane did not retain locks after guardian crash'
                print('PASS: pane retained both locks after guardian SIGKILL')
            if '--crash-pane' in sys.argv:
                os.kill(pane_pid, signal.SIGKILL)
                time.sleep(.5)
                held = [locked(path) for path in paths]
                result = subprocess.run(['ps', '-o', 'stat=', '-p', str(child_pid)],
                                        capture_output=True, text=True, check=False)
                state = result.stdout.strip()
                alive = bool(state and not state.startswith('Z'))
                print(f'Pane SIGKILL: locks={held}, shell_alive={alive}, shell_state={state or "absent"}')
                if alive and not all(held):
                    os.kill(child_pid, signal.SIGKILL)
                    print('OBSERVED: a surviving child can lose both locks when the pane wrapper is killed')
                    return
                # This is the child created by this probe, not an arbitrary process.
                if any(held):
                    os.kill(child_pid, signal.SIGKILL)
                wait_for(lambda: not any(locked(path) for path in paths), 'locks released after child exit')
                return
            subprocess.run([*command, 'kill-server'], check=True)
            wait_for(lambda: not any(locked(path) for path in paths), 'locks released after session exit')
            print('PASS: locks released after the disposable tmux session stopped')
        finally:
            if dual_holder and dual_holder.poll() is None:
                dual_holder.kill()
                dual_holder.wait()
            server.close()
            for handle in handles:
                handle.close()
            subprocess.run([tmux, '-L', name, 'kill-server'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--pane':
        pane(sys.argv[2], '--resistant' in sys.argv)
    elif len(sys.argv) == 3 and sys.argv[1] == '--guardian':
        guardian(sys.argv[2])
    else:
        main()
