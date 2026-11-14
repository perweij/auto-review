"""Shared single-attempt thread mutation and durable read-back evidence."""
import uuid
from .common import ApiError, now


def change(session, state, item, desired, refresh, current, on_pending=lambda: None):
    from .forge import adapter, restore
    forge = adapter(restore(session.manifest).forge)
    if not hasattr(forge, 'conversations') or ('resolve' if desired else 'reopen') not in forge.conversations.capabilities:
        raise ApiError('unsupported_operation', 'Thread mutations are unsupported by this forge.')
    if item['is_resolved'] is desired:
        accept_observed(state, item)
        session.save(state)
        return True, False
    if item.get('can_resolve' if desired else 'can_unresolve') is not True:
        raise ApiError('permission_denied', 'This account cannot change the conversation state.')
    # A new explicit request supersedes earlier intent for this thread only.
    for previous in pending(state):
        if previous['item'] == item['key']:
            previous['status'] = 'superseded'
    prior = state.get('thread_intent')
    if prior:
        state.setdefault('thread_history', []).append(prior)
    intent = dict(item=item['key'], resolved=desired, id=uuid.uuid4().hex, at=now(), status='submitting')
    state['thread_intent'] = intent
    on_pending()
    session.save(state)
    uncertain = False
    try:
        forge.conversations.set_state(session, item['key'], desired)
    except ApiError as exc:
        intent['status'] = 'ambiguous'
        session.save(state)
        if exc.code == 'cancelled':
            raise
        uncertain = True
    intent["response_uncertain"] = uncertain
    try:
        refresh()
    except ApiError as exc:
        intent['status'] = 'ambiguous'
        session.save(state)
        if exc.code == 'cancelled':
            raise
        raise ApiError('uncertain_thread_state', 'Conversation action sent; refresh to verify its current state. No retry sent.') from exc
    observed = current()
    confirmed = observed is not None and not observed.get('missing') and observed.get('is_resolved') is desired
    intent.update(status='confirmed' if confirmed else 'ambiguous', response_uncertain=uncertain)
    if confirmed and state.get('thread_intent') is intent:
        state.setdefault('thread_history', []).append(intent)
        state.pop('thread_intent', None)
    session.save(state)
    return confirmed, uncertain


def pending(state):
    return [intent for intent in [*state.get('thread_history', []), state.get('thread_intent')]
            if intent and intent.get('status', 'submitting') in ('submitting', 'ambiguous')]


def reconcile(state, items):
    by_key = {item['key']: item for item in items}
    for intent in pending(state):
        item = by_key.get(intent.get('item'))
        if item and item.get('is_resolved') is intent.get('resolved') and not item.get('missing'):
            intent.update(status='confirmed', observed_at=now())
    intent = state.get('thread_intent')
    if intent and intent.get('status') in ('confirmed', 'superseded'):
        state.setdefault('thread_history', []).append(intent)
        state.pop('thread_intent', None)


def accept_observed(state, item):
    """An explicit already-satisfied request retires contrary pending intent."""
    for intent in pending(state):
        if intent['item'] == item['key'] and intent['resolved'] is not item['is_resolved']:
            intent['status'] = 'superseded'
    reconcile(state, [item])
