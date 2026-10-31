"""Plan before coding, then use the production task loop with one repair.

Explicit live experiment: python planned.py /absolute/fresh/output --live
Only the recorded configuration step is evaluated; no PR writes or live state.
"""
import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from run import HERE, PROVENANCE, prepare, oracle, save
from pr_review import owner_fixes, owner_git, owner_providers
from pr_review.repository import Repository


def experiment(root):
    checkout, common = prepare(root, 'baseline')
    artifacts = root / 'artifacts'
    artifacts.mkdir()
    repo = Repository(checkout, common, 'github.com/evaluation/fixture', 11, 'origin')
    head = repo.git('rev-parse', 'HEAD').stdout.strip()
    recorded = json.loads((HERE / 'repair-context.json').read_text())
    state = {'worktree': str(checkout), 'branch': 'candidate',
             'metadata': {'baseRefOid': head, 'headRefOid': head},
             'trusted_configuration': owner_git.configuration_snapshot(checkout),
             'feedback': recorded['feedback'], 'issue_workflow': 'evaluation'}
    session = SimpleNamespace(path=artifacts, repo=repo,
        save=lambda value: save(artifacts / 'session.json', value),
        items=lambda value, keys: copy.deepcopy([i for i in value['feedback'] if i['key'] in keys]))
    choice = {'provider': 'codex', 'model': PROVENANCE['model'], 'effort': PROVENANCE['effort']}
    batch = {'settings': {'fix': choice, 'review': choice}, 'attempts': [],
             'tasks': [], 'baseline': head, 'repairs': 0}
    state['batch'] = batch
    batch['checkpoint'] = owner_git.checkpoint(session, state, 'baseline input')
    batch['git_admin'] = owner_git.admin(session, state)
    constraints = (
        'Use only the assigned checkout and attempt scratch. Do not inspect parent/sibling '
        'directories or evaluator files, use network/GitHub/MCP, spawn agents, or install dependencies. '
        'This experiment evaluates only the recorded configuration-contract step, not the entire '
        'approved issue. Plan exactly one task within that recorded step, with behavioral cases '
        'derived from its requirements and baseline source. Later orchestration steps are out of scope. '
        'The full approved issue is context and a scope boundary, not a request to implement all of it.')
    original = owner_fixes.context
    def context(*args):
        data = original(*args)
        data.update(evaluation_constraints=constraints, recorded_step=recorded['task'])
        return data
    summary = {'model': choice, 'calls': [], 'starting_tree': batch['checkpoint']['tree']}
    start = time.monotonic()
    with patch.object(owner_fixes, 'context', side_effect=context):
        try:
            data = context(session, state, batch, [i['key'] for i in state['feedback']], head)
            plan = owner_providers.invoke(session, state, batch, 'plan', data)
            tasks = owner_fixes.validate_plan(plan, state['feedback'])
            save(artifacts / 'plan.json', plan)
            if len(tasks) != 1:
                raise ValueError('Evaluation scope requires one configuration task; no automatic replan.')
            batch['tasks'] = tasks
            session.save(state)
            summary['task_passed'] = owner_fixes.run_task(session, state, batch, tasks[0])
        except Exception as exc:
            summary['error'] = str(exc)
    summary['seconds'] = round(time.monotonic() - start, 1)
    # External checks are invoked only after all model calls have finished.
    summary['external_checks'] = oracle(checkout, extended=True)
    for number, attempt in enumerate(batch['attempts']):
        path = Path(attempt['path'])
        rows = []
        events = path / 'events.jsonl'
        for line in events.read_text().splitlines() if events.exists() else []:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        summary['calls'].append({'role': attempt['role'], 'status': attempt['status'],
            'usage': [r['usage'] for r in rows if r.get('type') == 'turn.completed'],
            'tool_calls': sum(r.get('type') == 'item.completed' and r.get('item', {}).get('type') == 'command_execution' for r in rows)})
        if 'result' in attempt:
            save(artifacts / f'{number}-{attempt["role"]}.json', attempt['result'])
    final = owner_git.checkpoint(session, state, 'evaluated output')
    (artifacts / 'candidate.patch').write_text(repo.git('diff', head, final['tree']).stdout)
    save(artifacts / 'summary.json', summary)
    session.save(state)
    return summary


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[2] != '--live':
        raise SystemExit(__doc__)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.setdefault('PR_REVIEW_MODEL_TIMEOUT', '300')
    root = Path(sys.argv[1]).resolve()
    if root.exists():
        raise SystemExit('Use a fresh output directory.')
    print(json.dumps(experiment(root), indent=2))
