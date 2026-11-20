#!/usr/bin/env python3
"""Record forge-operation metadata, without logging credentials or request bodies."""
import fcntl
import math
import time
import json
import os
from pathlib import Path
import re
import subprocess
import sys

config = json.loads(Path(os.environ['SMOKE_TRACE_CONFIG']).read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
data = sys.stdin.buffer.read() if '--input' in args and args[args.index('--input')+1] == '-' else None
method, endpoint, query = ('POST' if any(a in args for a in ('--input', '-f', '-F', '--field', '--raw-field')) else 'GET'), None, None
for flag in ('--method', '-X'):
    if flag in args:
        method = args[args.index(flag)+1]
for arg in args:
    if arg.startswith(('repos/', 'projects/')) or arg in ('graphql', 'user', 'personal_access_tokens/self'):
        endpoint = arg
    if arg.startswith('query='):
        query = arg[6:]
if data:
    try:
        query = json.loads(data).get('query', query)
    except (ValueError, AttributeError):
        pass
kind = 'read' if method == 'GET' else 'mutation'
if endpoint == 'graphql':
    kind = 'unknown' if not isinstance(query, str) else 'mutation' if re.search(r'\bmutation\b', query) else 'read'
if endpoint is None:
    kind = 'metadata' if any(a in args for a in ('--version', '--help', 'status')) else 'unknown'
if tool == 'glab' and endpoint is not None and '--help' not in args:
    interval = float(config.get('gitlab_request_interval', 1.0))
    if not math.isfinite(interval) or not 0 <= interval <= 60:
        raise RuntimeError('GitLab smoke interval must be finite and between 0 and 60 seconds')
    stamp = Path(config['log']).with_name('gitlab-api-start.lock')
    with stamp.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        previous = float(lock.read() or '0')
        time.sleep(max(0, previous + interval - time.monotonic()))
        lock.seek(0)
        lock.truncate()
        lock.write(str(time.monotonic()))
        lock.flush()
result = subprocess.run([config[tool], *args], input=data, capture_output=True)
record = dict(tool=tool, actor=os.environ.get('SMOKE_ACTOR', 'fixture'), method=method,
              endpoint=endpoint, kind=kind, exit_code=result.returncode)
with Path(config['log']).open('a') as stream:
    stream.write(json.dumps(record)+'\n')
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
