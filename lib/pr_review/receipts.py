"""Canonical publication actions with read-only import of old action ledgers."""
import copy
from .common import read_json


def import_legacy(directory, head):
    result = []
    for item in read_json(directory / 'publication/actions.json', []):
        action = copy.deepcopy(item)
        if action.get('api'):
            # API batches already contain exact inline payloads. A projection
            # alone cannot reconstruct them; preserve it for recovery only.
            action['recovery_only'] = True
        action.update(comment_ids=action.get('proposal_ids', []), legacy=True)
        action['payload'] = {'body': action.get('body_md', action.get('content', ''))}
        if action['kind'] == 'review':
            action['payload'].update(commit_id=head, event=action['event'])
        if action['status'] == 'draft':
            action['status'] = 'superseded'  # Old implicit selections do not stage new work.
        result.append(action)
    return [{'id': 'legacy-import', 'actions': result, 'created_at': 'legacy'}] if result else []


def actions(directory, *, include_copied=False):
    from .state import generation
    manifest = read_json(directory / 'manifest.json', {})
    state = read_json(directory / 'publication/drafts.json', {})
    if state and (include_copied or state.get('generation') == generation(directory, manifest)):
        return [a for batch in state.get('batches', []) for a in batch['actions']]
    return read_json(directory / 'publication/actions.json', [])


def evidence(actions, adapter=None):
    """Read normalized own-activity evidence, including partial logical actions."""
    from .forge_github_publication import GitHubPublication
    for action in actions:
        for operation in action.get('operations', [action]):
            if operation.get('status') == 'published' or operation.get('receipt_verified'):
                if operation.get('evidence'):
                    yield operation['evidence']
                elif adapter is not None:
                    yield adapter.evidence(operation)
                elif 'version' not in operation:
                    yield GitHubPublication().evidence(operation)
                else:
                    from .common import ApiError
                    raise ApiError('incomplete_receipts', 'Missing normalized receipt evidence.')


def urls(actions, comments, adapter):
    values = [adapter.receipt_url(c['receipt']) for c in comments if c.get('receipt')]
    for action in actions:
        for operation in action.get('operations', [action]):
            if operation.get('result'):
                values.append(adapter.receipt_url(operation['result']))
    return values


def has_confirmed(action):
    return any(op.get('status') == 'published' or op.get('receipt_verified') for op in action.get('operations', [action]))
