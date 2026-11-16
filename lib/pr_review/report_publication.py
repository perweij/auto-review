"""Frozen execution-report snapshots, separate from finding/reply journals."""
import uuid

from .common import ApiError


class GitHubReports:
    """Narrow facade retaining existing GitHub report operation bytes."""
    retry_absent = True

    def __init__(self, publication):
        self.publication = publication

    def validate(self, session):
        pass  # The caller has already checked the GitHub account/destination.

    def prepare(self, session, key, body, viewer, identity):
        return self.publication.prepare_owner_reply(session,
            {'id': key, 'body': body, 'target': {'kind': 'timeline'}}, viewer, identity)

    def lookup(self, session, action):
        return self.publication.lookup_owner(session, [action])[action['id']]

    def submit(self, session, action):
        return self.publication.submit(session, action)

    def matches(self, session, action, receipt):
        return self.publication.matches(session, action, receipt)

    def receipt_url(self, action):
        return self.publication.owner_receipt(action['receipt'])['url']


def report_adapter(session):
    from .forge import adapter, require, restore
    project = restore(session.manifest)
    require(project.forge, 'report-publication')
    return adapter(project.forge).report_publication


def publish(session, actions, key, body, viewer, save):
    """Caller holds workflow locks and validates the destination/account first."""
    adapter = report_adapter(session)
    adapter.validate(session)
    for pending in actions.values():
        if pending['status'] not in ('submitting', 'ambiguous'):
            continue
        matches = adapter.lookup(session, pending)
        if (len(matches) > 1 or not matches and (pending.get('receipt') or not adapter.retry_absent)
                or matches and not adapter.matches(session, pending, matches[0])):
            raise ApiError('uncertain_report', 'An earlier report publication is uncertain or was edited; no snapshot was sent. Inspect the discussion and retry receipt lookup.')
        if matches:
            pending.update(status='published', receipt=matches[0])
        else:
            pending['status'] = 'draft'
        save()
    action = actions.get(key)
    if action is None:
        action = adapter.prepare(session, key, body, viewer, uuid.uuid4().hex)
        actions[key] = action
        save()
    if action['status'] != 'published':
        action['status'] = 'submitting'
        save()
        try:
            receipt = adapter.submit(session, action)
            action['receipt'] = receipt
            save()  # Retain response evidence even when validation fails.
            if not adapter.matches(session, action, receipt):
                raise ApiError('uncertain_report', 'Report response could not be verified; retry to recover its receipt.')
            action.update(status='published', receipt=receipt)
            save()
        except BaseException as exc:
            action['status'] = 'draft' if getattr(exc, 'report_not_sent', False) else 'ambiguous'
            save()
            raise
    return dict(report_publication='complete', report_url=adapter.receipt_url(action))
