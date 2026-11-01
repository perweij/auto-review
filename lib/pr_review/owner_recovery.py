"""Durable, issue-wide allowance for focused repairs after task-local repairs."""
import copy
import re

from .common import digest, now

ALLOWANCE = 2
RETRY = re.compile(r'/fix-issue retry ([a-f0-9]{16})')


def reserve(state, batch, task, review):
    if not state.get('issue_workflow') or not batch.get('automatic_recovery'):
        return None
    budget = state.setdefault('issue_recovery', {'limit': ALLOWANCE, 'attempts': [], 'authorizations': []})
    if len(budget['attempts']) >= budget['limit']:
        if not budget.get('request'):
            budget['request'] = {
                'token': digest([state['issue_workflow'], len(budget['attempts']),
                                 len(budget['authorizations'])])[:16], 'created_at': now()}
        task['recovery_request'] = copy.deepcopy(budget['request'])
        return None
    task.pop('recovery_request', None)
    record = {'batch': batch['id'], 'task': task['id'], 'at': now(),
              'tree': batch['checkpoint']['tree'], 'review': copy.deepcopy(review),
              'status': 'reserved'}
    budget['attempts'].append(record)
    return record


def authorize(state, comments, viewer):
    """Consume only the initiator's reply to the currently outstanding request."""
    budget = state.get('issue_recovery', {})
    request = budget.get('request')
    if not request:
        return False
    for comment in comments:
        if (comment.get('user', {}).get('login', '').lower() == viewer.lower()
                and (comment.get('body') or '').strip() == '/fix-issue retry ' + request['token']
                and comment.get('created_at', '') >= request['created_at']):
            budget['authorizations'].append({'request': request, 'receipt': copy.deepcopy(comment)})
            budget['limit'] += ALLOWANCE
            budget.pop('request')
            return True
    return False
