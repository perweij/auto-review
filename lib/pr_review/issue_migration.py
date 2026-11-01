"""Read pre-harness issue records; never execute their task or recovery policies."""
import copy
from .common import digest, now
from . import owner_contracts


def batch(value):
    """Preserve completed boundaries and archive unfinished legacy work once.

    Includes paused runs such as issue 10. Source adoption is checked by the
    shared workspace guards before this conversion; migration never adopts edits.
    """
    if value.get('execution_kind') == 'issue_harness':
        # Early harness versions stored an unfinished synthetic task. Its status
        # must not control resumption of the actual implementation conversation.
        pending = [t for t in value.get('tasks', []) if t['status'] != 'complete']
        if not pending:
            return
    else:
        pending = [t for t in value.get('tasks', []) if t['status'] != 'complete']
    old = copy.deepcopy(value.get('tasks', []))
    retained = [t for t in old if t['status'] == 'complete']
    before = (pending[0].get('recovery_baseline') or pending[0].get('before') or value['checkpoint']) if pending else (retained[-1]['after'] if retained else value['checkpoint'])
    if old:
        value.setdefault('plans', []).append({'at': now(), 'previous_tasks': old, 'migration': 'issue_harness'})
    value['tasks'] = retained
    value.setdefault('harness_before', before)
    if value.get('execution_kind') != 'issue_harness':
        value.update(execution_kind='issue_harness', harness_phase='implement')


def reviewed_questions(batch):
    """Only current independent review can promote a claim to a user question."""
    tree = batch.get('checkpoint', {}).get('tree')
    if not tree:
        return []
    reports = []
    for task in batch.get('tasks', []):
        review = task.get('review', {})
        implementation = task.get('implementation', {})
        if (task.get('after', {}).get('tree') == tree
                and owner_contracts.matches(review, owner_contracts.task_cases(task), tree)
                and (not any(o.get('status') == 'clarify' for o in implementation.get('outcomes', []))
                     or task.get('assessment_review_report') == digest(implementation))):
            reports.append(review)
    review = batch.get('review', {})
    if (review.get('case_tree', batch.get('review_tree')) == tree
            and owner_contracts.matches(review, owner_contracts.combined(batch.get('tasks', [])), tree)):
        reports.append(review)
    return list(dict.fromkeys(o['evidence'] for report in reports for o in report.get('outcomes', [])
                             if o.get('status') == 'clarify'))


def questions(flow):
    """Retire old fixer-only blockers; retain their history and all user replies."""
    from .issues import required_batches
    from . import owner_state
    from .owner_completion import execution_complete
    from .common import ApiError, locked
    state = flow.state
    blockers = state.get('execution_blockers')
    if not blockers or not state.get('owner'):
        return
    owner = owner_state.OwnerSession({'session': state['owner']})
    with locked(owner.execution_lock):
        data = owner.load()
        if data.get('issue_workflow') != state['id']:
            raise ApiError('invalid_session', 'Owner workflow identity changed.')
        batch = next((b for b in reversed(list(required_batches(data))) if not execution_complete(b)), None)
        if not batch or batch.get('execution_kind') == 'issue_harness' or reviewed_questions(batch):
            return
        proposals = [o['evidence'] for task in batch.get('tasks', [])
                     if task.get('status') == 'needs_attention'
                     for o in task.get('implementation', {}).get('outcomes', [])
                     if o.get('status') == 'clarify']
        if not proposals or any(b not in proposals for b in blockers):
            return
    state.setdefault('retired_execution_blockers', []).append({
        'blockers': blockers, 'at': now(), 'reason': 'Pending independent clarification assessment'})
    state.pop('execution_blockers')
    # Reassess with the original discussion, including any human replies.
    # Do not reuse a cached question or bypass plan approval/freshness checks.
    state['reassessment_generation'] = state.get('reassessment_generation', 0) + 1
    flow.save()
