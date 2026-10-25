"""Exercise operation-ID cancellation over the real JSON process transport."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

cli = sys.argv[1]
fixture = Path(os.environ["FIXTURE"])
request = (json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else
           {"protocol": 1, "command": "run", "pr": 7, "cwd": str(fixture / "checkout")})
with (fixture / "api-operation.log").open("w+") as log:
    child = subprocess.Popen([cli, "api"], stdin=subprocess.PIPE, stdout=log, stderr=log, text=True)
    try:
        child.stdin.write(json.dumps(request) + "\n")
        child.stdin.close()
        deadline = time.monotonic() + 25
        while not (fixture / "model-started").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Model did not start")
            time.sleep(0.05)
        log.seek(0)
        events = [json.loads(line) for line in log if line.strip()]
        identity = next(e["operation_id"] for e in events if e["type"] == "started")
        result = subprocess.run([cli, "api"], input=json.dumps({"protocol": 1, "command": "cancel", "operation_id": identity}) + "\n",
                                text=True, capture_output=True, check=True, timeout=5)
        assert json.loads(result.stdout)["result"]["cancelled"] is True
        assert child.wait(timeout=18) != 0
        log.seek(0)
        events = [json.loads(line) for line in log if line.strip()]
        assert events[-1]["error"]["code"] == "cancelled", events[-1]
        print("Owned operation cancelled and joined; saved review remains resumable.")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=18)
