#!/usr/bin/env python3
"""Forward one forge command, optionally injecting one journal-aware transport loss."""
import json
import os
from pathlib import Path
import subprocess
import sys

path = Path(os.environ['SMOKE_FAULT_FILE'])
config = json.loads(path.read_text())
args = sys.argv[1:]
data = sys.stdin.buffer.read() if '--input' in args and args[args.index('--input')+1] == '-' else None
method = 'GET'
for flag in ('--method', '-X'):
    if flag in args:
        method = args[args.index(flag)+1]
body = (data or b'').decode('utf-8', errors='replace')
endpoint_matches = config.get('endpoint') in args or bool(config.get('endpoint_suffix') and any(a.endswith(config['endpoint_suffix']) for a in args))
if config.get('used') and config.get('fail_readback') and not config.get('readback_failed') and 'graphql' in args and 'isDraft' in body and 'query' in body:
    config['readback_failed'] = True
    path.write_text(json.dumps(config))
    print('Smoke injected readback connection loss', file=sys.stderr)
    raise SystemExit(1)
matched = not config.get('used') and method == config['method'] and endpoint_matches and (not config.get('body_contains') or config['body_contains'] in body)
if matched:
    config['used'] = True
    path.write_text(json.dumps(config))
    if config['mode'] == 'before':
        print('Smoke injected connection loss before transmission', file=sys.stderr)
        raise SystemExit(1)
result = subprocess.run([config['executable'], *args], input=data, capture_output=True)
if matched:
    config['forwarded_exit'] = result.returncode
    config['response_bytes'] = len(result.stdout)
    path.write_text(json.dumps(config))
    if result.returncode == 0:
        if config['mode'] == 'empty':
            raise SystemExit(0)
        print('Smoke injected connection loss after accepted write', file=sys.stderr)
        raise SystemExit(1)
sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
