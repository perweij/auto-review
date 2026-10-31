"""One bounded repair and independent review per arm; no live PR operations.

Usage: python run.py /absolute/new/output [--live]
Without --live, only reconstruct and check the frozen controls.
"""
import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'lib'))
from pr_review import owner_contracts, owner_fixes, owner_git, owner_providers
from pr_review.repository import Repository


def command(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True)


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(root, kind):
    root.mkdir(parents=True)
    seed = root / 'seed'
    seed.mkdir()
    archive = command(['git', 'archive', PROVENANCE['base']], ROOT).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(seed, filter='data')
    command(['git', 'init', '-q', '-b', 'fixture'], seed)
    command(['git', 'config', 'user.name', 'Owner evaluation'], seed)
    command(['git', 'config', 'user.email', 'evaluation@example.invalid'], seed)
    command(['git', 'add', '-A'], seed)
    command(['git', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Frozen baseline'], seed)
    checkout = root / 'checkout'
    command(['git', 'worktree', 'add', '-qb', 'candidate', str(checkout)], seed)
    name = 'before_repair' if kind in ('control', 'treatment') else 'failed_repair'
    if kind != 'baseline':
        command(['git', 'apply', str(HERE / (name + '.patch'))], checkout)
    if kind == 'manual-reference':
        command(['git', 'apply', str(HERE / 'manual-reference.patch')], checkout)
    return checkout, seed / '.git'


def oracle(checkout, *, extended=False):
    result = command([sys.executable, str(HERE / 'check_cases.py'), str(checkout),
                      *(['--extended'] if extended else [])], check=False)
    if result.returncode not in (0, 1) or not result.stdout:
        raise RuntimeError(result.stderr.decode())
    return json.loads(result.stdout)


def replay(root, arm):
    checkout, common = prepare(root, arm)
    artifacts = root / 'artifacts'
    artifacts.mkdir()
    repo = Repository(checkout, common, 'github.com/evaluation/fixture', 11, 'origin')
    head = repo.git('rev-parse', 'HEAD').stdout.strip()
    state = {'worktree': str(checkout), 'branch': 'candidate',
             'metadata': {'baseRefOid': head, 'headRefOid': head},
             'trusted_configuration': owner_git.configuration_snapshot(checkout)}
    session = SimpleNamespace(path=artifacts, repo=repo,
                              save=lambda value: save(artifacts / 'session.json', value))
    choice = {'provider': 'codex', 'model': PROVENANCE['model'], 'effort': PROVENANCE['effort']}
    batch = {'settings': {'fix': choice, 'review': choice}, 'attempts': []}
    state['batch'] = batch
    batch['checkpoint'] = owner_git.checkpoint(session, state, 'frozen input')
    batch['git_admin'] = owner_git.admin(session, state)
    context = json.loads((HERE / 'repair-context.json').read_text())
    context.update(baseline=head, batch_diff=repo.git('diff', head, batch['checkpoint']['tree']).stdout,
                   completed_tasks=[], evaluation_constraints=(
                       'Use only the assigned checkout and your attempt scratch for source investigation. '
                       'Do not inspect parent/sibling directories or evaluator files, use network/GitHub/MCP, '
                       'spawn agents, or install dependencies. This is a bounded repair replay.'))
    task = context['task']
    keys = [item['key'] for item in context['feedback']]
    if arm == 'treatment':
        context['verification_cases'] = []
    summary = {'arm': arm, 'model': choice, 'calls': [], 'before': oracle(checkout)}
    start = time.monotonic()
    with ExitStack() as stack:
        if arm == 'control':
            original = owner_providers.context_prompt
            addition = (ROOT / 'prompts/owner-compatibility.txt').read_text() + '\n'
            stack.enter_context(patch.object(owner_providers, 'context_prompt',
                side_effect=lambda instructions, data, directory: original(instructions.replace(addition, ''), data, directory)))
            stack.enter_context(patch.object(owner_providers, 'output_schema',
                side_effect=lambda role, _: json.loads((HERE / ('legacy-' + role + '.schema.json')).read_text())))
        try:
            fixed = owner_fixes.validate_result(owner_providers.invoke(session, state, batch, 'fix', context), keys)
            if arm == 'treatment':
                owner_contracts.implementation(task, fixed, batch['checkpoint']['tree'])
            save(artifacts / 'fix.json', fixed)
            if not fixed['complete'] or any(o['status'] == 'clarify' for o in fixed['outcomes']):
                raise RuntimeError('Repair incomplete or needs clarification; no extra retry.')
            review_context = copy.deepcopy(context)
            review_context.pop('review_findings', None)
            review_context.update(implementation_report=fixed,
                batch_diff=repo.git('diff', head, batch['checkpoint']['tree']).stdout)
            if arm == 'treatment':
                review_context['verification_cases'] = owner_contracts.task_cases(task)
            reviewed = owner_fixes.validate_result(owner_providers.invoke(session, state, batch, 'review', review_context), keys)
            if arm == 'treatment':
                owner_contracts.bind(reviewed, owner_contracts.task_cases(task), batch['checkpoint']['tree'])
            save(artifacts / 'review.json', reviewed)
            summary['review_passed'] = owner_fixes.successful(reviewed)
        except Exception as exc:
            summary['error'] = str(exc)
    summary['seconds'] = round(time.monotonic() - start, 1)
    summary['after'] = oracle(checkout)
    for attempt in batch['attempts']:
        events = Path(attempt['path']) / 'events.jsonl'
        rows = []
        for line in events.read_text().splitlines() if events.exists() else []:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        summary['calls'].append({'role': attempt['role'], 'status': attempt['status'],
            'usage': [r['usage'] for r in rows if r.get('type') == 'turn.completed'],
            'tool_calls': sum(r.get('type') == 'item.completed' and r.get('item', {}).get('type') == 'command_execution' for r in rows)})
    final = owner_git.checkpoint(session, state, 'evaluated output')
    (artifacts / 'repair.patch').write_text(repo.git('diff', batch['attempts'][0]['before']['tree'], final['tree']).stdout)
    save(artifacts / 'summary.json', summary)
    return summary


PROVENANCE = json.loads((HERE / 'provenance.json').read_text())
if __name__ == '__main__':
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.setdefault('PR_REVIEW_MODEL_TIMEOUT', '300')
    output = Path(sys.argv[1]).resolve()
    output.mkdir(exist_ok=False)
    controls = {}
    for name in ('failed', 'manual-reference'):
        checkout, _ = prepare(output / name, name)
        controls[name] = oracle(checkout)
    save(output / 'controls.json', controls)
    assert any(not row['passed'] for row in controls['failed'])
    assert all(row['passed'] for row in controls['manual-reference']), controls['manual-reference']
    print('Frozen failure and manual reference verified.', flush=True)
    if '--live' in sys.argv:
        for arm in ('control', 'treatment'):
            summary = replay(output / arm, arm)
            print(json.dumps(summary), flush=True)
