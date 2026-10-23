"""Run a CLI with actual TTY stdin; feed menu answers only after each prompt."""
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time

answers = iter(sys.argv[1].split("|"))
master, slave = pty.openpty()
child = subprocess.Popen(sys.argv[2:], stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True)
os.close(slave)
pending = b""
deadline = time.monotonic() + 45
try:
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("PTY test timed out")
        readable, _, _ = select.select([master], [], [], 0.2)
        if not readable:
            continue
        try:
            data = os.read(master, 65536)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        pending += data
        if pending.endswith((b"Type yes: ", b"Choose action [n]: ")):
            os.write(master, (next(answers, "n") + "\n").encode())
            pending = b""
    sys.exit(child.wait(timeout=5))
finally:
    os.close(master)
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
