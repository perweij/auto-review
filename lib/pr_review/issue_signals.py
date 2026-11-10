"""Controller-owned, nonblocking command acknowledgments and approval guidance."""
from .common import ApiError, digest
from .events import emit


def commands(flow):
    from .issues import approval_revision, matches_receipt
    receipts = [a['receipt'] for a in flow.state.get('actions', {}).values() if a.get('receipt')]
    for comment in flow.facts['comments']:
        revision = approval_revision(comment, flow.state)
        if revision and not any(matches_receipt(comment, r) for r in receipts):
            yield comment, revision


def acknowledge(flow):
    ledger = flow.state.setdefault('command_signals', {})
    for comment, revision in commands(flow):
        key = str(comment['id'])
        entry = ledger.setdefault(key, {})
        if entry.get('reaction'):
            continue
        try:
            result = flow.writes.api(f'repos/{flow.repo.name}/issues/comments/{comment["id"]}/reactions', {'content': 'eyes'}, timeout=10)
            if not isinstance(result, dict) or result.get('content') != 'eyes' or not result.get('id'):
                raise ApiError('reaction_failed', 'Invalid reaction receipt.')
            entry['reaction'] = result
            entry.pop('error', None)
        except (ApiError, ValueError, TypeError) as exc:
            if getattr(exc, 'code', None) in ('cancelled', 'resource_limit', 'integrity_failed'):
                raise
            entry['error'] = str(exc)
            emit('progress', message='Could not acknowledge issue command; retrying on a later run: ' + str(exc))
        flow.save()


def explain(flow):
    from .issues import approval_comment, matches_receipt
    state = flow.state
    ledger = state.setdefault('command_signals', {})
    for comment, revision in commands(flow):
        entry = ledger.setdefault(str(comment['id']), {})
        version = digest(comment.get('body', ''))
        accepted = entry.setdefault('accepted', [])
        receipt = state.get('approval', {}).get('receipt')
        if version in accepted or receipt and matches_receipt(comment, receipt):
            if version not in accepted:
                accepted.append(version)
                flow.save()
            continue
        plan = state.get('plan')
        available = plan and state.get('waiting') != 'answers' and state.get('phase') not in ('complete', 'blocked', 'ready')
        if available and approval_comment(comment, state):
            accepted.append(version)
            flow.save()
            continue
        if available:
            current = plan['revision']
            body = (f'Approval for revision {revision} cannot be accepted: '
                    + ('it predates publication of the plan.' if revision == str(current) else f'the current revision is {current}.')
                    + f' Please review revision {current} and post `/fix-issue approve {current}` in a new comment.')
        else:
            body = f'Approval for revision {revision} cannot be accepted because no plan is currently awaiting approval. '
            body += 'Please answer the outstanding questions first.' if state.get('waiting') == 'answers' else 'Wait for a new plan approval request before approving.'
        key = 'approval-rejected:' + digest([comment['id'], version, body])
        flow.writes.comment(body, key)
