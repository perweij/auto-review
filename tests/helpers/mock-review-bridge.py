"""Relay mock-only observations out of the private reviewer namespace."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])


def handle(message):
    kind = message['kind']
    if kind == 'call':
        with (root / 'calls.jsonl').open('a') as stream:
            stream.write(message['value'] + '\n')
    elif kind == 'stage':
        stage = message['stage']
        with (root / 'stages').open('a') as stream:
            stream.write(stage + '\n')
        (root / ('environment-' + stage)).write_text(message['environment'])
        with (root / 'reviewed-heads').open('a') as stream:
            stream.write(message['head'] + '\n')
    elif kind == 'marker':
        (root / 'model-started').touch()
    elif kind == 'move-ref':
        remote = str(root / 'remote.git')
        ref = message['ref']
        if ref not in ('refs/heads/main', 'refs/pull/7/head'):
            raise ValueError('unexpected fixture ref')
        git = ['git', '--git-dir=' + remote]
        if message.get('head'):
            new = message['head']
        else:
            parent = subprocess.check_output(git + ['rev-parse', ref], text=True).strip()
            env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com',
                       GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.com')
            new = subprocess.check_output(git + ['commit-tree', parent + '^{tree}', '-p', parent],
                                          input='advanced\n', text=True, env=env).strip()
        subprocess.run(git + ['update-ref', ref, new], check=True)
    else:
        raise ValueError('unexpected fixture request')


while True:
    with (root / 'mock-bridge-request').open() as incoming:
        line = incoming.readline()
    if not line:
        continue
    try:
        message = json.loads(line)
        if message['kind'] == 'stop':
            break
        handle(message)
        result = 'ok'
    except Exception as exc:
        result = 'error: ' + str(exc).replace('\n', ' ')
    with (root / 'mock-bridge-response').open('w') as response:
        response.write(result + '\n')
