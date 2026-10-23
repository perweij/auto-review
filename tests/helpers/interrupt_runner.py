"""Interrupt a sleeping model through its orchestrator and enforce test liveness."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

fixture = Path(os.environ["FIXTURE"])
with (fixture / "interrupt-output").open("w+") as log:
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while not (fixture / "model-started").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("model never started")
            time.sleep(0.05)
        child.send_signal(signal.SIGINT)
        rc = child.wait(timeout=10)
        log.seek(0)
        print(log.read())
        sys.exit(rc)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
