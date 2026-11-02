#!/usr/bin/env python3
"""Opt-in paid smoke: file/shell access in fresh and resumed implementation turns.

Uses the production issue-harness command builder in a disposable Git repository.
No GitHub access or publication. Logs and effective argv are retained locally.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import issue_harness, providers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['claude', 'codex'], required=True)
    parser.add_argument('--model', required=True)
    options = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='provider-policy-smoke-'))
    repo, binary = root / 'repo', root / 'bin'
    repo.mkdir()
    binary.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    # A PATH sentinel detects attempted nested wrapper launches.
    sentinel = binary / 'bwrap'
    sentinel.write_text('#!/bin/sh\necho invoked >> "' + str(root / 'bwrap-invoked') + '"\nexit 97\n')
    sentinel.chmod(0o700)
    env, _ = providers.environment(options.provider)
    env['PATH'] = str(binary) + os.pathsep + env['PATH']
    schema = root / 'schema.json'
    schema.write_text(json.dumps({'type': 'object', 'properties': {'proof': {'type': 'string'}},
                                 'required': ['proof'], 'additionalProperties': False}))
    conversation = {'session_id': str(uuid.uuid4())} if options.provider == 'claude' else {}
    choice = dict(provider=options.provider, model=options.model, effort='low')
    records = []
    print('Smoke artifacts: ' + str(root), flush=True)
    for turn in ('fresh', 'resumed'):
        directory = root / turn
        (directory / 'scratch').mkdir(parents=True)
        token = uuid.uuid4().hex
        (repo / 'input.txt').write_text(token + '\n')
        args = issue_harness.arguments(choice, directory, schema, conversation)
        prompt = ('Read input.txt in the current directory. Execute this harmless shell command: '
                  f'printf executed > shell-proof-{turn}. The JSON proof field must contain exactly the token read from input.txt, '
                  'not the shell-proof file contents or explanatory prose. Do not edit any other file or contact any external service.')
        result = subprocess.run(args, cwd=repo, env=env, input=prompt, text=True,
                                capture_output=True, timeout=180)
        (directory / 'stdout.jsonl').write_text(result.stdout)
        (directory / 'stderr.txt').write_text(result.stderr)
        records.append(dict(turn=turn, argv=args, exit_status=result.returncode))
        (root / 'commands.json').write_text(json.dumps(records, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(f'{turn} failed; inspect {directory}')
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        if options.provider == 'codex':
            value = json.loads((directory / 'result.json').read_text())
            ids = [e['thread_id'] for e in events if e.get('type') == 'thread.started']
            if ids:
                conversation['session_id'] = ids[-1]
        else:
            value = next(e['structured_output'] for e in events if e.get('type') == 'result')
        assert value['proof'].strip() == token, 'Provider did not read the current file'
        assert (repo / f'shell-proof-{turn}').read_text() == 'executed', 'Shell command did not execute'
        assert not (root / 'bwrap-invoked').exists(), 'Nested bubblewrap was invoked'
        assert conversation.get('session_id'), 'No implementation session to resume'
        conversation['started'] = True
        print(f'{turn}: file read and shell command passed; no PATH bubblewrap invocation', flush=True)
    (root / 'result.json').write_text(json.dumps(dict(provider=options.provider, model=options.model,
                                                    fresh=True, resumed=True, bwrap_invoked=False)) + '\n')


if __name__ == '__main__':
    main()
