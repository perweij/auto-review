"""Bounded behavioral cases and evidence identities for Owner verification.

Evidence is a provider report, not a backend attestation that commands ran.
The backend enforces coverage, consistency and identity; review stays independent.
"""
import copy
import re
from .common import ApiError, digest

MAX_CASES = 8


def cases(value, *, code='invalid_result'):
    if not isinstance(value, list) or len(value) > MAX_CASES:
        raise ApiError(code, 'Supply at most eight focused verification cases per task.')
    seen = set()
    for case in value:
        if (not isinstance(case, dict) or not isinstance(case.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', case['id']) or case['id'] in seen
                or case.get('intent') not in ('preserve', 'change')
                or any(not isinstance(case.get(k), str) or not case[k].strip()
                       for k in ('scenario', 'expected', 'basis'))):
            raise ApiError(code, 'Verification cases need unique IDs, intent, scenario, expectation and supporting basis.')
        seen.add(case['id'])
    return copy.deepcopy(value)


def task_cases(task):
    return copy.deepcopy(task.get('verification_cases', []))


def combined(tasks):
    return [{**case, 'id': task['id'] + '/' + case['id']} for task in tasks
            if task['status'] == 'complete' for case in task_cases(task)]


def bind(report, required, tree):
    """Validate new evidence and bind it to cases and the actual source checkpoint."""
    supplied = report.get('case_results')
    if required or supplied is not None:
        if not isinstance(supplied, list) or any(not isinstance(r, dict) for r in supplied):
            raise ApiError('invalid_result', 'Report verification evidence for every required case.')
        if any(not isinstance(r.get('id'), str) for r in supplied):
            raise ApiError('invalid_result', 'Case evidence IDs must be strings.')
        if sorted(r.get('id', '') for r in supplied) != sorted(c['id'] for c in required):
            raise ApiError('invalid_result', 'Case evidence must cover each required case exactly once.')
        for row in supplied:
            if (row.get('status') not in ('tested', 'source_traced', 'failed', 'unavailable')
                    or not isinstance(row.get('evidence'), str) or not row['evidence'].strip()):
                raise ApiError('invalid_result', 'Every case requires a supported status and concrete evidence.')
        if any(r['status'] in ('failed', 'unavailable') for r in supplied) and report.get('complete') and not report.get('blocking'):
            raise ApiError('invalid_result', 'Failed or unavailable case evidence cannot support a passed outcome.')
        if any(r['status'] == 'unavailable' for r in supplied) and report.get('assessment') == 'changes_requested':
            raise ApiError('invalid_result', 'Unavailable verification evidence requires an incomplete assessment.')
    if required or supplied is not None:
        report.update(case_contract=digest(required), case_tree=tree)
    return report


def implementation(task, report, tree):
    """A fixer may add cases, never rewrite existing requirements during a repair."""
    previous = task_cases(task)
    proposed = cases(report.get('verification_cases', []))
    by_id = {c['id']: c for c in previous}
    for case in proposed:
        if case['id'] in by_id and by_id[case['id']] != case:
            raise ApiError('invalid_result', 'Repair changed an existing verification case; clarify or explicitly replan.')
        by_id[case['id']] = case
    required = cases(list(by_id.values()))
    bind(report, required, tree)
    if required:
        task['verification_cases'] = required
        task['case_contract_version'] = 1
    return report


def matches(report, required, tree):
    if not required and 'case_contract' not in report:
        return True  # Historical reports did not supply case-level evidence.
    if report.get('case_contract') != digest(required) or report.get('case_tree') != tree:
        return False
    try:
        bind(copy.deepcopy(report), required, tree)
        return True
    except (ApiError, TypeError):
        return False


def verified(report, required, tree):
    if not required and 'case_contract' not in report:
        return True
    return (matches(report, required, tree) and report.get('complete') is True
            and not report.get('blocking')
            and all(r['status'] in ('tested', 'source_traced') for r in report.get('case_results', [])))


def current(batch):
    for task in batch.get('tasks', []):
        if task['status'] == 'complete' and not verified(task.get('review', {}), task_cases(task), task.get('after', {}).get('tree')):
            return False
    return verified(batch.get('review', {}), combined(batch.get('tasks', [])), batch.get('final_tree'))


def require_current(batch):
    if not current(batch):
        raise ApiError('stale_verification', 'Verification cases or source changed; reverify before committing or publishing.')
