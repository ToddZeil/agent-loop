"""Give the interactive shell a controlling PTY without preexec_fn in a threaded server."""
import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.execvpe(sys.argv[1], [sys.argv[1], '-i'], os.environ)
