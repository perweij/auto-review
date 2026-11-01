"""Read-only Codex subscription telemetry; no model turns or token inference."""
import json
import selectors
import shutil
import subprocess
import time
from .processes import stop_group


def read_codex(timeout=10):
    executable = shutil.which('codex')
    if not executable:
        return None
    try:
        child = subprocess.Popen([executable, 'app-server', '--listen', 'stdio://'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None
    try:
        def send(value):
            child.stdin.write((json.dumps(value)+'\n').encode())
            child.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'fix-issue-quota', 'version': '1'}}})
        pending = b''
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                for key, _ in selector.select(.1):
                    import os
                    data = os.read(key.fd, 65536)
                    if not data:
                        return None
                    pending += data
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        event = json.loads(line)
                        if event.get('id') == 1:
                            if 'error' in event:
                                return None
                            send({'method': 'initialized'})
                            send({'id': 2, 'method': 'account/rateLimits/read'})
                        if event.get('id') == 2:
                            result = event.get('result', {})
                            pools = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits', {})}
                            windows = {}
                            for pool, limits in pools.items():
                                for name in ('primary', 'secondary'):
                                    value = limits.get(name) or {}
                                    used = value.get('usedPercent')
                                    if (value.get('windowDurationMins') == 10080 and type(used) in (int, float)
                                            and 0 <= used <= 100 and value.get('resetsAt')):
                                        windows[pool + ':' + name] = {'used': used, 'reset': value['resetsAt']}
                            return windows or None
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    finally:
        stop_group(child)
        child.stdin.close()
        child.stdout.close()
