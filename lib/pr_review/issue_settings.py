"""Stopped issue execution choices, separate from completed evidence provenance."""
import copy
import uuid

from .common import ApiError, now
from .events import emit


def reruns(request):
    value = request.get('rerun_review', [])
    if (not isinstance(value, list) or not value or
            any(stage not in ('reviewer-1', 'reviewer-2', 'synthesis') for stage in value)):
        if value != []:
            raise ApiError('invalid_request', 'rerun_review must list reviewer-1, reviewer-2 or synthesis.')
    if value and request.get('retry_review_adjudication'):
        raise ApiError('invalid_request', 'Choose review regeneration or retrying the saved adjudication, rather than both.')
    return list(dict.fromkeys(value))


def change_batch(session, state, batch):
    """Caller holds execution_lock and has checked the retained checkpoint."""
    from .issue_harness import require_supported
    from .owner_state import issue_settings, effective_settings
    if issue_settings(batch['settings']) == issue_settings(state['settings']):
        return
    require_supported(batch)
    if batch.get('commit_execution'):
        raise ApiError('settings_changed', 'Finish the saved commit transaction before changing its execution settings.')
    before = copy.deepcopy(batch['settings'])
    batch.setdefault('review_identity_settings', copy.deepcopy(before))
    policy = effective_settings(state['settings'])['final_review']['synthesis_tests']
    if effective_settings(before)['final_review']['synthesis_tests'] != policy:
        batch['review_identity_settings'] = effective_settings(batch['review_identity_settings'])
        batch['review_identity_settings']['final_review']['synthesis_tests'] = policy
    batch['settings'] = copy.deepcopy(state['settings'])
    batch.setdefault('settings_transitions', []).append({
        'id': uuid.uuid4().hex, 'at': now(), 'before': before,
        'after': copy.deepcopy(batch['settings']), 'checkpoint': copy.deepcopy(batch['checkpoint'])})
    session.save(state)
    emit('progress', message='Execution settings updated; completed evidence keeps its original model attribution.')


def select_conversation(state, specification, choice):
    """Reuse same-provider conversations; provider changes get a fresh generation."""
    from .common import digest
    conversations = state.setdefault('issue_harness_sessions', {})
    active = state.get('issue_harness_active_session')
    # The exact old key provides a conservative migration for saved sessions.
    old_key = digest([state['issue_workflow'], specification, choice])
    if not active and old_key in conversations:
        active = old_key
    if not active:
        matches = [key for key, saved in conversations.items()
                   if saved.get('choice', {}).get('provider') == choice['provider']
                   and key == digest([state['issue_workflow'], specification, saved['choice']])]
        if len(matches) == 1:
            active = matches[0]
    previous = conversations.get(active, {})
    spec = digest(specification)
    if (previous and previous.get('choice', {}).get('provider') == choice['provider']
            and previous.get('specification', spec) == spec):
        key, conversation = active, previous
        handoff = None
    else:
        key = uuid.uuid4().hex
        conversation = conversations[key] = {'choice': copy.deepcopy(choice), 'specification': spec}
        handoff = ({'previous_session': active, 'previous_choice': copy.deepcopy(previous.get('choice')),
                    'reason': 'provider changed' if previous.get('specification', spec) == spec else 'specification changed'}
                   if previous else None)
        if handoff:
            conversation['handoff'] = handoff
    state['issue_harness_active_session'] = key
    conversation['choice'] = copy.deepcopy(choice)
    conversation['specification'] = spec
    return key, conversation, handoff


def validate_rerun_boundary(batch):
    from .owner_completion import execution_complete
    if execution_complete(batch) or batch.get('commit_execution') or batch.get('commit_progress'):
        raise ApiError('invalid_request', 'Review regeneration is unavailable after execution completes or committing begins; completed results retain their original evidence.')
    authorization = batch.get('review_adjudication_authorization', {})
    rounds = batch.get('review_history', {}).get('rounds', [])
    latest = rounds[-1] if rounds else {}
    verdict = latest.get('projected', latest.get('raw', {}))
    cycles = batch.get('review_history', {}).get('cycles', [])
    # Allocation saves phase 'implement'; a completed repair report restores
    # 'review', including repairs that add evidence without editing source.
    repaired = (cycles and cycles[-1]['status'] == 'allocated'
                and cycles[-1]['origin'] == latest.get('id') and batch.get('harness_phase') == 'review')
    # Ingestion is durable before the controller allocates a repair or records a
    # stop. Protect that crash window as well as the explicit saved decisions.
    rejected = (latest.get('candidate', {}).get('tree') == batch['checkpoint']['tree']
                and verdict.get('assessment') in ('changes_requested', 'needs_input')
                and not verdict.get('verification_blockers') and not repaired)
    if (batch.get('review_stop') or batch.get('review_decision')
            or authorization.get('status') == 'pending' or rejected
            or batch.get('harness_phase') in ('implement', 'adjudication', 'review_decision')):
        raise ApiError('invalid_request', 'The saved review requires repair or adjudication; resume normally or follow its recovery action before regenerating evidence.')


def validate_rerun(batch, configuration, stages):
    validate_rerun_boundary(batch)
    from .issue_harness import require_supported
    from .owner_state import effective_settings
    require_supported(batch)
    if (batch.get('harness_candidate', {}).get('status') != 'ready'
            or batch.get('harness_candidate_tree') != batch['checkpoint']['tree']):
        raise ApiError('invalid_request', 'No completed implementation candidate is available to review; resume normally.')
    available = {s['id'] for s in effective_settings(configuration)['final_review']['reviewer_slots']}
    if any(s != 'synthesis' and s not in available for s in stages):
        raise ApiError('invalid_request', 'Choose a configured reviewer slot or synthesis to rerun.')


def request_rerun(session, state, batch, stages, token):
    """Allocate once per invocation intent, before changing review evidence."""
    reruns({'rerun_review': stages})
    if not isinstance(token, str) or not token:
        raise ApiError('invalid_request', 'Use issue-run or fix-issue --rerun-review to record a review regeneration request.')
    if not stages or token in batch.get('review_rerun_requests', {}):
        return
    validate_rerun(batch, batch['settings'], stages)
    affected = list(dict.fromkeys([*stages, 'synthesis']))
    batch.setdefault('review_rerun_requests', {})[token] = affected
    for stage in affected:
        batch.setdefault('review_reruns', {})[stage] = token
    batch['harness_phase'] = 'review'
    batch['status'] = 'paused'
    session.save(state)


def retain_review_choices(run):
    """Check old completed evidence under its original choices, never relabel it."""
    from . import cache, review_engine, settings
    requested = copy.deepcopy(run.settings)
    phases = run.manifest['phases']
    versions = {}
    run.retained_versions = versions
    def inputs(stage):
        value = phases.get(stage, {}).get('inputs')
        return value if isinstance(value, dict) else {}
    def eligible(stage):
        phase = phases.get(stage, {})
        expected = run.batch.get('review_reruns', {}).get(stage)
        prior = inputs(stage)
        return (phase.get('status') == 'completed' and isinstance(prior.get('cli_version'), str)
                and phase.get('fingerprint') == cache.fingerprint(prior)
                and prior.get('review_rerun') == expected)
    def retain_versions():
        for stage, phase in phases.items():
            if eligible(stage):
                versions[stage] = phase['inputs']['cli_version']
    retain_versions()
    # A completed synthesis already covers its original slot topology. Extra or
    # removed slots configure future work, unless regeneration was requested.
    previous = run.previous_configuration
    if previous and eligible('synthesis'):
        run.settings = copy.deepcopy(previous)
        run.settings['synthesis_tests'] = requested['synthesis_tests']
        slots = run.settings['reviewer_slots']
        slots_valid = (not any(s['id'] in phases for s in slots) or
                       all(cache.reusable(run, s['id'], run.path / 'reviews' / (s['id'] + '.md'),
                                          cache.inputs(run, s['id'])) for s in slots))
        if slots_valid:
            run.adapter.render_synthesis(run)
            if review_engine.reusable_synthesis(run, run.adapter, run.path / 'reviews/synthesis.json') is not None:
                if run.settings != requested:
                    emit('progress', message='Keeping the completed acceptance review under its recorded settings; changed choices apply to future review work.')
                return
    run.settings = requested
    for slot in list(requested['reviewer_slots']):
        stage = slot['id']
        old = inputs(stage).get('slot')
        if not old or not eligible(stage):
            versions.pop(stage, None)
            continue
        final = {'reviewer_slots': [copy.deepcopy(old) if s['id'] == stage else copy.deepcopy(s)
                                    for s in run.settings['reviewer_slots']],
                 'synthesis': run.settings['processing'], 'synthesis_tests': run.settings['synthesis_tests']}
        before = run.settings
        run.settings = settings.final_review_configuration(final)
        if not cache.reusable(run, stage, run.path / 'reviews' / (stage + '.md'), cache.inputs(run, stage)):
            run.settings = before
            versions.pop(stage, None)
        elif old != slot:
            emit('progress', message=f"Keeping completed {stage}: {old['provider']} / {old['model']} ({old['effort']}); changed choices apply when it next needs to run.")
    versions.pop('synthesis', None)  # Unfinished synthesis always uses the requested choice.
