"""Issue-only, durable review lineage and bounded repair transitions.

All mutations are committed by the caller's single Owner-state save. Artifact-local
IDs never identify a concern across rounds; only explicit validated links do.
"""
import copy
from pathlib import Path

from .common import ApiError, digest
from .storage import hash_file

VERSION = 1
FIELDS = {'finding_lineage', 'previous_dispositions'}


def enabled(batch):
    if batch.get('review_control_version') is not None and (type(batch['review_control_version']) is not int or batch['review_control_version'] != VERSION):
        raise ApiError('unsupported_session', 'Unsupported issue review-control version.')
    return batch.get('review_control_version') == VERSION


def controls(request):
    if 'max_repair_cycles' in request and (type(request['max_repair_cycles']) is not int or request['max_repair_cycles'] < 1):
        raise ApiError('invalid_request', 'max_repair_cycles must be a positive integer.')
    if 'retry_review_adjudication' in request and type(request['retry_review_adjudication']) is not bool:
        raise ApiError('invalid_request', 'retry_review_adjudication must be a Boolean.')


def initialize(batch, request):
    batch.update(review_control_version=VERSION, max_repair_cycles=request.get('max_repair_cycles', 3),
                 review_history={'version': VERSION, 'revision': 0, 'next_id': 1, 'findings': {},
                                 'rounds': [], 'cycles': [], 'synthesis_inputs': {}, 'input_heads': {}})


def ledger(batch):
    value = batch.get('review_history')
    if (not isinstance(value, dict) or value.get('version') != VERSION
            or any(type(value.get(k)) is not int for k in ('revision', 'next_id'))
            or any(not isinstance(value.get(k), dict) for k in ('findings', 'synthesis_inputs'))
            or any(not isinstance(value.get(k), list) for k in ('rounds', 'cycles'))):
        raise ApiError('invalid_state', 'Unsupported issue review ledger.')
    return value


def snapshot(batch, identity):
    """Freeze before synthesis. Re-ingestion must not invalidate its own cache."""
    history = ledger(batch)
    base = digest([identity, history['cycles'][-1]['id'] if history['cycles'] else 0])
    heads = history.setdefault('input_heads', {})
    key = heads.setdefault(base, base)
    previous = next((r for r in reversed(history['rounds']) if r['input_key'] == key), None)
    if previous and not previous['completed']:
        key = heads[base] = digest([base, history['revision']])
    inputs = history['synthesis_inputs']
    if key not in inputs:
        inputs[key] = public_snapshot(batch)
    return key, copy.deepcopy(inputs[key])


def base_value(value):
    return {k: v for k, v in value.items() if k not in FIELDS}


def invalid(message):
    raise ApiError('invalid_result', 'Review lineage: ' + message)


def validate(value, frozen):
    if not isinstance(value, dict) or not FIELDS <= set(value):
        invalid('finding_lineage and previous_dispositions are required.')
    prior = frozen['findings']
    lineage, dispositions = value['finding_lineage'], value['previous_dispositions']
    if not isinstance(lineage, list) or not isinstance(dispositions, list):
        invalid('lineage and dispositions must be arrays.')
    links, used = {}, set()
    local = {f['id']: f for f in value['findings']}
    for link in lineage:
        if not isinstance(link, dict) or set(link) != {'local_id', 'prior_id'}:
            invalid('each lineage needs local_id and prior_id (null for new).')
        identifier, previous = link['local_id'], link['prior_id']
        if not isinstance(identifier, str) or identifier not in local or identifier in links:
            invalid('unknown or duplicate local finding ID.')
        if previous is not None and (not isinstance(previous, str) or previous not in prior or previous in used):
            invalid('unknown or duplicate stable finding ID.')
        links[identifier] = previous
        if previous is not None:
            used.add(previous)
    if set(links) != set(local):
        invalid('every current finding needs exactly one lineage entry.')
    decisions = {}
    for decision in dispositions:
        if (not isinstance(decision, dict) or set(decision) != {'finding_id', 'status', 'evidence', 'reason'}
                or not isinstance(decision['finding_id'], str) or decision['finding_id'] not in prior
                or decision['finding_id'] in decisions or decision['status'] not in ('open', 'resolved', 'rejected')
                or any(not isinstance(decision[k], str) or not decision[k].strip() for k in ('evidence', 'reason'))):
            invalid('unknown, duplicate or unevidenced previous disposition.')
        decisions[decision['finding_id']] = decision
    required = {key for key, f in prior.items() if f['status'] == 'open' and f['blocking']}
    if not required <= set(decisions):
        invalid('every previously open blocker needs a disposition.')
    by_stable = {previous: local[key] for key, previous in links.items() if previous is not None}
    for key, decision in decisions.items():
        current = by_stable.get(key)
        if decision['status'] == 'open' and (current is None or not current['blocking']):
            invalid('an open disposition needs a current blocking finding.')
        if decision['status'] != 'open' and current is not None and current['blocking']:
            invalid('a closed disposition contradicts a current blocker.')
    for key, finding in by_stable.items():
        if finding['blocking']:
            decision = decisions.get(key)
            if decision is None or decision['status'] != 'open':
                invalid('linked blockers need an explicit open disposition.')
            if prior[key]['status'] != 'open':
                old_evidence = {o['finding']['evidence'] for o in prior[key]['observations']}
                if finding['evidence'] in old_evidence or decision['evidence'] in old_evidence:
                    invalid('reopening a closed concern requires new evidence and a reason.')
    return value


def ingest(batch, value, frozen, candidate, artifact, input_key):
    history = ledger(batch)
    identity = digest([candidate, value, input_key])
    previous = next((r for r in history['rounds'] if r['id'] == identity), None)
    if previous:
        return previous
    validate(value, frozen)
    cycle = history['cycles'][-1] if history['cycles'] else None
    complete = value['assessment'] in ('passed', 'changes_requested') and not value.get('verification_blockers')
    cycle_id = cycle['id'] if cycle else 0
    decisions = {d['finding_id']: d for d in value['previous_dispositions']}
    for key, decision in decisions.items():
        history['findings'][key]['status'] = decision['status']
        history['findings'][key].setdefault('dispositions', []).append({'round': identity, **copy.deepcopy(decision)})
    links, disputed = {}, []
    for link in value['finding_lineage']:
        finding = next(f for f in value['findings'] if f['id'] == link['local_id'])
        key = link['prior_id']
        if key is None:
            key = 'R' + str(history['next_id'])
            history['next_id'] += 1
            history['findings'][key] = {'id': key, 'original': copy.deepcopy(finding), 'observations': [],
                                        'dispositions': [], 'survived_cycles': []}
        entry = history['findings'][key]
        prior_status = frozen['findings'].get(key, {}).get('status')
        if finding['blocking'] and prior_status in ('resolved', 'rejected'):
            disputed.append(key)
            entry['survived_cycles'] = []
        entry.update(status='open' if finding['blocking'] else decisions.get(key, {}).get('status', entry.get('status') if entry.get('status') in ('resolved', 'rejected') else 'resolved'),
                     blocking=finding['blocking'])
        entry['observations'].append({'round': identity, 'candidate': copy.deepcopy(candidate), 'cycle': cycle_id,
                                      'finding': copy.deepcopy(finding)})
        if (finding['blocking'] and cycle and complete and key in cycle['blockers']
                and cycle_id not in entry['survived_cycles']):
            entry['survived_cycles'].append(cycle_id)
        if finding['blocking'] and len(entry['survived_cycles']) >= 2:
            disputed.append(key)
        if not finding['blocking']:
            entry['survived_cycles'] = []
        links[finding['id']] = key
    record = {'id': identity, 'candidate': copy.deepcopy(candidate), 'artifact': str(artifact),
              'artifact_hash': hash_file(Path(artifact)), 'input_key': input_key,
              'cycle': cycle_id, 'completed': complete, 'lineage': links,
              'dispositions': copy.deepcopy(value['previous_dispositions']), 'disputes': sorted(set(disputed)),
              'raw': copy.deepcopy(value)}
    history['rounds'].append(record)
    history['revision'] += 1
    if cycle and complete:
        cycle.update(status='complete', completed_round=identity)
    return record


def allocate(batch, round_record, feedback):
    history = ledger(batch)
    existing = next((c for c in history['cycles'] if c['origin'] == round_record['id']), None)
    if existing:
        batch.update(harness_phase='implement', harness_feedback=feedback)
        return existing
    used = len(history['cycles'])
    if used >= batch['max_repair_cycles']:
        stop(batch, 'repair_limit', round_record, 'The repair-cycle allowance is exhausted.')
    cycle = {'id': used + 1, 'origin': round_record['id'], 'blockers':
             [round_record['lineage'][f['id']] for f in round_record.get('projected', round_record['raw'])['findings'] if f['blocking']], 'status': 'allocated'}
    history['cycles'].append(cycle)
    batch.update(harness_phase='implement', harness_feedback=feedback)
    batch.pop('review_stop', None)
    batch.pop('review_decision', None)
    return cycle


def stop(batch, code, round_record, reason, *, context=None):
    details = {'stage': 'adjudication' if code == 'review_conflict' else 'repair',
               'stop_reason': code, 'disputed_findings': round_record['disputes'],
               'repair_cycles_used': len(ledger(batch)['cycles']), 'max_repair_cycles': batch['max_repair_cycles'],
               'artifact': round_record['artifact'], 'summary': reason,
               'next_action': 'inspect_review_conflict' if code == 'review_conflict' else 'increase_repair_cycles'}
    batch['review_stop'] = {'code': code, 'round': round_record['id'], 'context': context, 'details': details}
    raise ApiError(code, reason, **details)


def raise_stop(batch):
    saved = batch['review_stop']
    details = {**saved['details'], 'max_repair_cycles': batch['max_repair_cycles']}
    raise ApiError(saved['code'], details['summary'], **details)


def schema_fields():
    from .owner_providers import obj, TEXT
    return {'finding_lineage': {'type': 'array', 'items': obj({'local_id': TEXT, 'prior_id': {'type': ['string', 'null']}})},
            'previous_dispositions': {'type': 'array', 'items': obj({'finding_id': TEXT,
                'status': {'type': 'string', 'enum': ['open', 'resolved', 'rejected']}, 'evidence': TEXT, 'reason': TEXT})}}


def context_identity(session, state, batch):
    """Verify evidence/spec bytes even for an inert, pre-authentication resume."""
    from . import specification, test_evidence, acceptance
    document = specification.handoff(session, state)
    refs = test_evidence.references(batch)
    test_evidence.index(refs)
    return digest({'tree': batch['checkpoint']['tree'], 'baseline': batch['baseline'],
        'specification': document['locator'], 'feedback': [specification.provenance(i) for i in session.items(state, batch['items'])],
        'trust': [state.get('trusted_snapshot'), state.get('trusted_configuration')],
        'settings': batch.get('review_identity_settings', batch['settings']),
        **({'review_reruns': batch['review_reruns']} if batch.get('review_reruns') else {}),
        'evidence': refs, 'report': batch.get('harness_candidate'),
        'instructions': {k: v for k, v in acceptance._PROMPTS.items() if 'acceptance' in k or k.startswith('synthesis-testing-')},
        'validator': [acceptance.VALIDATOR_VERSION, VERSION]})


def resume_guard(session, state, batch, request):
    """Shared entry guard; runs before discovery/auth. Returns on authorized work."""
    controls(request)
    if not enabled(batch):
        if request.get('retry_review_adjudication') or 'max_repair_cycles' in request:
            raise ApiError('invalid_request', 'Review controls apply only to newly created issue batches; this batch retains legacy behavior.')
        return
    if 'max_repair_cycles' in request:
        # Lowering is allowed, including below consumed cycles: completed/allocated
        # work remains valid, and the next allocation must fit the new allowance.
        batch['max_repair_cycles'] = request['max_repair_cycles']
    saved = batch.get('review_stop')
    current = context_identity(session, state, batch)
    if batch.get('review_decision') and batch['review_decision']['context'] != current:
        batch.pop('review_decision')
        batch['harness_phase'] = 'review'
    if saved and saved.get('context') != current:
        batch.pop('review_stop', None)
        batch['harness_phase'] = 'review'
        saved = None
    authorization = batch.get('review_adjudication_authorization')
    if authorization and authorization['status'] == 'pending':
        if authorization['context'] == current:
            batch['harness_phase'] = 'adjudication'
            session.save(state)
            return
        batch.pop('review_adjudication_authorization')
    if request.get('retry_review_adjudication'):
        if not saved or saved['code'] != 'review_conflict':
            raise ApiError('invalid_request', 'There is no retryable saved review conflict for the current candidate/context.')
        record = next(r for r in ledger(batch)['rounds'] if r['id'] == saved['round'])
        if not record.get('retry_pending'):
            record['adjudication_generation'] = record.get('adjudication_generation', 0) + 1
            record['retry_pending'] = True
        batch['review_adjudication_authorization'] = {'round': record['id'],
            'generation': record['adjudication_generation'], 'context': current, 'status': 'pending'}
        batch['harness_phase'] = 'adjudication'
        session.save(state)
        return
    if saved:
        if saved['code'] == 'repair_limit' and len(ledger(batch)['cycles']) < batch['max_repair_cycles']:
            batch['harness_phase'] = 'review'
            return
        record = next(r for r in ledger(batch)['rounds'] if r['id'] == saved['round'])
        if record.get('retry_pending'):
            batch['harness_phase'] = 'adjudication'
            return
        session.save(state)
        raise_stop(batch)


def public_snapshot(batch):
    """Model-facing ledger: provenance remains, internal frozen-input copies do not."""
    history = ledger(batch)
    value = copy.deepcopy({k: history[k] for k in ('version', 'revision', 'findings', 'rounds', 'cycles')})
    for record in value['rounds']:
        for request in record.get('adjudications', {}).values():
            request.pop('model_input', None)
    return value


def resume_command(issue, repository, code, details):
    command = f"fix-issue --issue {issue} --repo {repository}"
    if code == 'repair_limit':
        command += f" --max-repair-cycles {max(details['max_repair_cycles'], details['repair_cycles_used']) + 1}"
    elif code == 'review_conflict':
        command += ' --retry-review-adjudication'
    return command
