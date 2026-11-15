"""Frozen execution-report snapshots, separate from finding/reply journals."""
import uuid

from .common import ApiError
from .publication import publication_adapter


def publish(session, actions, key, body, viewer, save):
    """Caller holds workflow locks and validates the destination/account first."""
    adapter = publication_adapter(session)
    for pending in actions.values():
        if pending['status'] not in ('submitting', 'ambiguous'):
            continue
        matches = adapter.lookup_owner(session, [pending])[pending['id']]
        if len(matches) > 1 or not matches and pending.get('receipt'):
            raise ApiError('uncertain_report', 'An earlier report publication is uncertain; no new snapshot was sent. Retry recovery after checking the PR discussion.')
        if matches:
            pending.update(status='published', receipt=matches[0])
        else:
            pending['status'] = 'draft'
        save()
    action = actions.get(key)
    if action is None:
        action = adapter.prepare_owner_reply(session,
            {'id': key, 'body': body, 'target': {'kind': 'timeline'}}, viewer, uuid.uuid4().hex)
        actions[key] = action
        save()
    if action['status'] != 'published':
        action['status'] = 'submitting'
        save()
        try:
            receipt = adapter.submit(session, action)
            if not adapter.matches(session, action, receipt):
                raise ApiError('uncertain_report', 'Report response could not be verified; retry to recover its receipt.')
            action.update(status='published', receipt=receipt)
            save()
        except BaseException:
            action['status'] = 'ambiguous'
            save()
            raise
    return dict(report_publication='complete', report_url=adapter.owner_receipt(action['receipt'])['url'])
