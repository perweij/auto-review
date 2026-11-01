"""Bounded replanning for verification cases blocked by downstream tasks."""
import copy
from collections import Counter

from .common import ApiError, digest, now


def prerequisites(task, tasks):
    found, pending = set(), list(task.get('depends_on', []))
    by_id = {t['id']: t for t in tasks}
    while pending:
        key = pending.pop()
        if key not in found:
            found.add(key)
            pending.extend(by_id.get(key, {}).get('depends_on', []))
    return found


def proposal(state, batch, task):
    report = task.get('review', {})
    dependencies = report.get('plan_dependencies', [])
    if (not state.get('issue_workflow') or not batch.get('automatic_recovery')
            or batch.get('verify_only') or report.get('complete') is not False
            or any(o.get('status') == 'clarify' for o in report.get('outcomes', []))
            or not dependencies):
        return None
    if not isinstance(dependencies, list):
        raise ApiError('invalid_result', 'Planning dependencies must be a list.')
    pending = [t for t in batch['tasks'] if t['status'] not in ('complete', 'skipped')]
    by_id = {t['id']: t for t in pending}
    unavailable = {r['id'] for r in report.get('case_results', []) if r.get('status') == 'unavailable'}
    case_ids = {c['id'] for c in task.get('verification_cases', [])}
    for row in dependencies:
        if (not isinstance(row, dict) or not isinstance(row.get('case_id'), str)
                or not isinstance(row.get('requires_task'), str)
                or row['case_id'] not in unavailable & case_ids
                or row['requires_task'] not in by_id
                or task['id'] not in prerequisites(by_id[row['requires_task']], batch['tasks'])
                or not isinstance(row.get('evidence'), str) or not row['evidence'].strip()):
            raise ApiError('invalid_result', 'Planning recovery requires an unavailable case and a downstream unfinished prerequisite.')
    retained = []
    for old in pending:
        for case in old.get('verification_cases', []):
            retained.append({'task': old['id'], 'items': list(old['items']), 'original_id': case['id'],
                             'case': {**copy.deepcopy(case), 'id': 'r-' + digest([old['id'], case['id']])[:24]}})
    return {'blocked_task': task['id'], 'dependencies': copy.deepcopy(dependencies),
            'retained_cases': retained,
            'requirements': [{'task': t['id'], **copy.deepcopy(e)} for t in pending for e in t['expected']]}


def reserve(state, batch, proposal):
    # One planning-repair operation per issue workflow, independent of the code
    # repair allowance. Persist before invoking the planner; crashes consume it.
    if state.get('issue_plan_recovery'):
        return None
    record = {'at': now(), 'batch': batch['id'], 'tree': batch['checkpoint']['tree'],
              'proposal': copy.deepcopy(proposal), 'status': 'reserved'}
    state['issue_plan_recovery'] = record
    return record


def validate(tasks, proposal):
    supplied = [c for t in tasks for c in t.get('verification_cases', [])]
    counts = Counter(digest(c) for c in supplied)
    owners = {}
    for retained in proposal['retained_cases']:
        case = retained['case']
        if counts[digest(case)] != 1 or sum(c['id'] == case['id'] for c in supplied) != 1:
            raise ApiError('invalid_plan', 'Planning repair must retain each supplied case exactly once, unchanged.')
        owners[retained['task'], retained['original_id']] = next(
            t['id'] for t in tasks if case in t.get('verification_cases', []))
        owner = next(t for t in tasks if t['id'] == owners[retained['task'], retained['original_id']])
        if not set(retained['items']) <= set(owner['items']):
            raise ApiError('invalid_plan', 'Retained cases must still cover their original feedback items.')
    requirements = {}
    for old in proposal['requirements']:
        matches = [t['id'] for t in tasks for e in t['expected']
                   if e['item'] == old['item'] and old['outcome'] in e['outcome']]
        if len(matches) != 1:
            raise ApiError('invalid_plan', 'Retain every original task outcome verbatim in exactly one replacement outcome; concatenate when merging.')
        requirements.setdefault(old['task'], set()).update(matches)
    by_id = {t['id']: t for t in tasks}
    for dependency in proposal['dependencies']:
        target = owners[proposal['blocked_task'], dependency['case_id']]
        ready = prerequisites(by_id[target], tasks) | {target}
        if not requirements[dependency['requires_task']] <= ready:
            raise ApiError('invalid_plan', 'The blocked case still precedes its required implementation.')


def install(batch, plan, tasks):
    previous = copy.deepcopy(batch['tasks'])
    retained = [t for t in batch['tasks'] if t['status'] in ('complete', 'skipped')]
    prefix = f"p{len(batch['plans']) + 1}-"
    unfinished = [t for t in previous if t['status'] not in ('complete', 'skipped')]
    if unfinished and unfinished[0].get('before'):
        tasks[0]['recovery_baseline'] = copy.deepcopy(unfinished[0]['before'])
    for task in tasks:
        task['id'] = prefix + task['id']
        task['depends_on'] = [prefix + d for d in task['depends_on']]
        # All completed prerequisites stay satisfied; their records are untouched.
        task['depends_on'] += [t['id'] for t in retained if t['status'] == 'complete'
                               and set(t['items']) & set(task['items'])]
        # Repartitioning must not replenish consumed task-local repair credit.
        task['repairs'] = max((t.get('repairs', 0) for t in previous
                              if t['status'] not in ('complete', 'skipped') and set(t['items']) & set(task['items'])), default=0)
    batch['plans'].append({'at': now(), 'previous_tasks': previous, 'plan': plan, 'automatic_dependency_repair': True})
    batch['tasks'] = retained + tasks
    batch['issue_task_format'] = 2
