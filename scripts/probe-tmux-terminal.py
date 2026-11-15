#!/usr/bin/env python3
"""Exercise an isolated tmux session without touching a supervisor worktree."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
TMUX = shutil.which('tmux')
if not TMUX:
    sys.exit('Install tmux to run this isolated probe.')
socket = 'agent-loop-probe-' + uuid.uuid4().hex


def tmux(*args):
    return subprocess.check_output([TMUX, '-L', socket, *args], stderr=subprocess.STDOUT, text=True)


def wait_for(fn, description, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except subprocess.CalledProcessError:
            pass
        time.sleep(.05)
    raise RuntimeError('Timed out waiting for ' + description)


def attach(cols, rows):
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    try:
        client = subprocess.Popen(
            [sys.executable, str(ROOT / 'loop' / 'terminal_child.py'), TMUX,
             '-L', socket, 'attach-session', '-t', 'probe'],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            env={**os.environ, 'TERM': 'xterm-256color'}, close_fds=True)
    finally:
        os.close(slave)
    return client, master


def drain(fd):
    out = bytearray()
    while select.select([fd], [], [], .1)[0]:
        try:
            out.extend(os.read(fd, 65536))
        except OSError:
            break
    return out


def dimensions():
    return tmux('display-message', '-p', '-t', 'probe', '#{pane_width}x#{pane_height}').strip()


client = None
master = None
try:
    tmux('new-session', '-d', '-s', 'probe', '/bin/sh')
    tmux('set-option', '-t', 'probe', 'status', 'off')
    pid = tmux('display-message', '-p', '-t', 'probe', '#{pane_pid}').strip()
    client, master = attach(90, 28)
    wait_for(lambda: dimensions() == '90x28', 'initial size')
    os.write(master, b"printf '\\033[?1049h\\033[2J\\033[HAGENT_LOOP_TMUX_PROBE'; echo READY\r")
    wait_for(lambda: 'AGENT_LOOP_TMUX_PROBE' in tmux('capture-pane', '-p', '-a', '-t', 'probe'), 'alternate screen')
    drain(master)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 100, 0, 0))
    os.kill(client.pid, signal.SIGWINCH)
    wait_for(lambda: dimensions() == '100x35', 'resized pane')
    os.close(master)
    master = None
    client.wait(timeout=5)
    client = None
    assert tmux('display-message', '-p', '-t', 'probe', '#{pane_pid}').strip() == pid
    client, master = attach(100, 35)
    output = wait_for(lambda: drain(master), 'reattachment output')
    assert b'AGENT_LOOP_TMUX_PROBE' in output, 'reattachment did not redraw the alternate screen'
    os.write(master, b"echo AFTER_REATTACH\r")
    wait_for(lambda: 'AFTER_REATTACH' in tmux('capture-pane', '-p', '-t', 'probe'), 'post-reattach input')
    print('PASS: alternate screen, resize, detach, same process, redraw and input after reattach')
finally:
    if master is not None:
        os.close(master)
    if client is not None and client.poll() is None:
        client.terminate()
        try:
            client.wait(timeout=3)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait()
    subprocess.run([TMUX, '-L', socket, 'kill-server'], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
