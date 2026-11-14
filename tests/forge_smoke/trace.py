#!/usr/bin/env python3
"""Record forge-operation metadata, without logging credentials or request bodies."""
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
result = subprocess.run([config[tool], *args], input=data, capture_output=True)
record = dict(tool=tool, actor=os.environ.get('SMOKE_ACTOR', 'fixture'), method=method,
              endpoint=endpoint, kind=kind, exit_code=result.returncode)
with Path(config['log']).open('a') as stream:
    stream.write(json.dumps(record)+'\n')
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
