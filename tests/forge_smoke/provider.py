#!/usr/bin/env python3
"""Scripted structured issue stages; review protocol delegates to existing doubles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

args = sys.argv[1:]
tool = Path(sys.argv[0]).name
fixture = Path(os.environ['FIXTURE'])
if '--help' in args:
    subprocess.run([str(fixture / 'delegates' / tool), *args], check=True)
    print('--sandbox --cd --ignore-rules --permission-prompts')
    raise SystemExit(0)
schema = None
if '--output-schema' in args:
    schema = json.loads(Path(args[args.index('--output-schema')+1]).read_text())
elif '--json-schema' in args:
    schema = json.loads(args[args.index('--json-schema')+1])
props = schema.get('properties', {}) if schema else {}
value = None
if 'decision' in props:
    value = dict(decision='plan', summary='Add a pure multiplication function with standard-library tests.',
        plan='## Outcome\nProvide total(price, quantity) returning their product.\n\n## Scope\nAdd smoke_total.py and test_smoke_total.py only; preserve README.\n\n## Approach\nImplement multiplication with no dependencies.\n\n## Acceptance scenarios\nThree items at four each cost twelve; zero quantity costs zero.\n\n## Verification\nRun python3 -m unittest test_smoke_total.\n',
        acceptance=['total(3, 4) == 12', 'total(3, 0) == 0', 'README unchanged'], questions=[],
        inspected=[dict(path='README.md', finding='Repository baseline contains README; add isolated module and test.')], decisions=[])
    if 'review_dispositions' in props:
        value['review_dispositions'] = []
elif 'points' in props:
    value = dict(summary='The bounded fixture specification is implementable.', points=[])
elif 'status' in props and 'test_evidence' in props:
    if not Path.cwd().resolve().is_relative_to(fixture.parent.resolve()):
        raise RuntimeError('Scripted fixer refuses to edit outside its run directory')
    Path('smoke_total.py').write_text('def total(price, quantity):\n    return price * quantity\n')
    Path('test_smoke_total.py').write_text('import unittest\nfrom smoke_total import total\nclass TotalTest(unittest.TestCase):\n    def test_product(self):\n        self.assertEqual(total(3, 4), 12)\n        self.assertEqual(total(3, 0), 0)\n')
    subprocess.run([sys.executable, '-m', 'unittest', 'test_smoke_total'], check=True, stdout=sys.stderr)
    value = dict(status='ready', summary='Implemented multiplication and tests.', checks=['python3 -m unittest test_smoke_total passed'], findings=[], questions=[], test_evidence=[])
elif 'assessment' in props and 'verification_blockers' in props:
    value = dict(assessment='passed', summary='Scripted acceptance of the bounded fixture.', checks=['Fixture implementation uses multiplication.'],
                 findings=[], outcomes=[], case_results=[], questions=[], verification_blockers=[])
    keys = props['outcomes']['items']['properties']['item'].get('enum', [])
    value['outcomes'] = [dict(item=key, status='fixed', evidence='Scripted fixture implements multiplication and standard-library tests.') for key in keys]
    for key in ('finding_lineage', 'previous_dispositions'):
        if key in props:
            value[key] = []
if value is None:
    os.execv(str(fixture / 'delegates' / tool), [tool, *args])
# Consume prompt but never write it to output or forge logs.
sys.stdin.read()
with (fixture / 'issue-stages.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(tool=tool, properties=list(props)))+'\n')
if tool == 'codex':
    sid = args[-2] if 'resume' in args else str(uuid.uuid4())
    print(json.dumps(dict(type='thread.started', thread_id=sid)))
    Path(args[args.index('-o')+1]).write_text(json.dumps(value))
    print(json.dumps(dict(type='turn.completed')))
else:
    flag = '--resume' if '--resume' in args else '--session-id'
    sid = args[args.index(flag)+1] if flag in args else str(uuid.uuid4())
    print(json.dumps(dict(type='system', subtype='init', session_id=sid)))
    print(json.dumps(dict(type='result', subtype='success', is_error=False, structured_output=value)))
