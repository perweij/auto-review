"""Local execution reports bound to new standalone Owner batches."""
from contextlib import ExitStack
from contextvars import ContextVar

from . import session_trace as trace, session_report, usage
from .common import ApiError, digest, read_json
from .events import emit

_active = ContextVar('owner_report_operation', default=None)


def root(session, batch):
    return session.path / 'batch-reports' / batch['id']


def identity(session, batch):
    return digest([str(session.path.resolve()), batch['id']])


def attach(session, state, batch, *, new=False):
    operation = _active.get()
    if operation is None or batch.get('execution_kind') == 'issue_harness':
        return
    if new:
        batch['execution_trace_version'] = 2
        session.save(state)
        trace.initialize(root(session, batch), identity(session, batch))
    if batch.get('execution_trace_version') != 2:
        return
    operation['batch'] = batch
    operation['stack'].enter_context(trace.operation(root(session, batch), True, identity(session, batch)))


def generate(session, state, batch):
    if batch.get('execution_trace_version') != 2 or batch.get('execution_kind') == 'issue_harness':
        raise ApiError('report_unavailable', 'Execution reports require a new standalone Owner batch.')
    journal = trace.validate(read_json(root(session, batch) / 'execution-trace.json'), identity(session, batch))
    calls = read_json(usage.owner_root(session, state) / 'usage/ledger.json', {}).get('calls', {})
    data = session_report.report_data(journal, calls, outcome=batch['status'], shared_ledger=True)
    data['owner'] = dict(batch=batch['id'], url=state.get('url'),
                         candidate=batch['checkpoint']['tree'], verify_only=batch.get('verify_only', False))
    fields, body = session_report.write_report(root(session, batch) / 'reports', data)
    fields.update(report_coverage=data['coverage'], report_publication='local-only')
    previous = batch.get('execution_report', {})
    fields.update({k: previous[k] for k in ('report_url', 'report_publication') if k in previous})
    batch['execution_report'] = fields
    return fields, body


def execute(function, session, state, request):
    operation = {'stack': ExitStack()}
    token = _active.set(operation)
    try:
        with operation['stack']:
            try:
                result = function(session, state, request)
            except BaseException as exc:
                batch = operation.get('batch')
                if batch:
                    source = trace.marked('acceptance_result') or batch.get('trace_candidate')
                    if getattr(exc, 'code', '') in ('invalid_result', 'integrity_failed'):
                        trace.invalidate(source, exc)
                    trace.decision('stopped/' + getattr(exc, 'code', 'interrupted'), sources=[source], summary=str(exc))
                raise
            else:
                batch = operation.get('batch')
                if batch:
                    trace.decision('boundary/' + batch['status'], sources=[trace.marked('acceptance_result') or batch.get('trace_candidate')])
    finally:
        _active.reset(token)
        batch = operation.get('batch')
        if batch:
            state = session.load()
            batch = next(b for b in state['batches'] if b['id'] == batch['id'])
            outcome = 'needs_attention' if batch['status'] == 'paused' else batch['status']
            trace.boundary(root(session, batch), identity(session, batch), {'outcome': outcome})
            try:
                generate(session, state, batch)
                batch.pop('report_warning', None)
                session.save(state)
            except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
                batch['report_warning'] = str(exc)
                emit('progress', message='Owner execution report unavailable: ' + str(exc))
                try:
                    session.save(state)
                except (ApiError, OSError):
                    pass

    if operation.get('batch') and isinstance(result, dict) and 'batches' in result:
        result['revision'] = state['sequence']
        for row in result['batches']:
            if row['id'] == batch['id']:
                row['execution_report'] = batch.get('execution_report', {})
                if batch.get('report_warning'):
                    row['report_warning'] = batch['report_warning']
    return result


def invoke(session, state, batch, role, context):
    """Record the validated model result and the exact inputs consumed."""
    from . import owner_providers, owner_fixes, owner_contracts
    import copy
    if batch.get('execution_trace_version') != 2:
        return owner_providers.invoke(session, state, batch, role, context)
    task_id = context.get('task', {}).get('id')
    task = next((t for t in batch['tasks'] if t['id'] == task_id), None)
    role_name = {'plan': 'planning', 'fix': 'implementation', 'review': 'task-check'}[role]
    sources = [batch.get('trace_plan'), batch.get('trace_candidate')]
    if task:
        sources += [task.get('trace_result')]
        sources += [t.get('trace_result') for t in batch['tasks'] if t['id'] in task['depends_on']]
    else:
        sources += [batch.get('trace_candidate'), trace.marked('acceptance_result')]
    with trace.stage(role_name, choice=batch['settings']['review' if role == 'review' else 'fix'],
                     inputs=sources, key=f"{batch['id']}:{task_id or 'combined'}:{role}",
                     context={'batch': batch['id'], 'task': task_id, 'cycle': batch['repairs']}) as handle:
        value = owner_providers.invoke(session, state, batch, role, context)
        if role == 'plan':
            try:
                owner_fixes.validate_plan(value, context['feedback'])
            except ApiError as exc:
                exc.rejected_plan = value
                raise
            outcome = 'plan'
        else:
            value = owner_fixes.validate_result(value, [i['key'] for i in context['feedback']])
            if role == 'fix' and task:
                owner_contracts.implementation(copy.deepcopy(task), value, batch['checkpoint']['tree'])
            else:
                owner_contracts.bind(value, context.get('verification_cases', []), batch['checkpoint']['tree'])
            if role == 'fix' and not task and value.get('verification_cases'):
                raise ApiError('invalid_result', 'Integration repair must preserve completed task contracts.')
            outcome = ('ready' if role == 'fix' else 'passed') if owner_fixes.successful(value) else 'needs_attention' 
        path = batch['attempts'][-1].get('path') if batch['attempts'] else None
        artifact = path + '/result.json' if path else None
        result = trace.complete(handle, {**value, 'status': outcome}, artifact)
        if role == 'plan':
            batch['trace_plan'] = result
        elif task:
            task['trace_result'] = result
        batch['trace_candidate'] = result
        session.save(state)
        return value


def publish(session, state, batch):
    """Publish an immutable snapshot; uncertain sends require read-back first."""
    import uuid
    from .publication import publication_adapter
    from .forge import check_account
    fields, body = generate(session, state, batch)
    _, account = session.repo.forge.owner_context(session.repo)
    check_account(state.get('forge_account'), account, state.get('viewer'))
    adapter = publication_adapter(session)
    journal = trace.validate(read_json(root(session, batch) / 'execution-trace.json'), identity(session, batch))
    key = digest([identity(session, batch), journal, batch['checkpoint']['tree'], batch['status']])
    actions = batch.setdefault('report_actions', {})
    for pending in actions.values():
        if pending['status'] in ('submitting', 'ambiguous'):
            matches = adapter.lookup_owner(session, [pending])[pending['id']]
            if len(matches) > 1 or not matches and pending.get('receipt'):
                raise ApiError('uncertain_report', 'An earlier report publication is uncertain; no new snapshot was sent. Retry recovery after checking the PR discussion.')
            if matches:
                pending.update(status='published', receipt=matches[0])
            else:
                pending['status'] = 'draft'  # Complete read-back establishes absence before retry.
            session.save(state)
    action = actions.get(key)
    if action is None:
        action = adapter.prepare_owner_reply(session, {'id': key, 'body': body, 'target': {'kind': 'timeline'}},
                                             state['viewer'], uuid.uuid4().hex)
        actions[key] = action
        session.save(state)
    if action['status'] != 'published':
        action['status'] = 'submitting'
        session.save(state)
        try:
            receipt = adapter.submit(session, action)
            if not adapter.matches(session, action, receipt):
                raise ApiError('uncertain_report', 'Report response could not be verified; retry to recover its receipt.')
            action.update(status='published', receipt=receipt)
            session.save(state)
        except BaseException:
            action['status'] = 'ambiguous'
            session.save(state)
            raise
    fields.update(report_publication='complete', report_url=adapter.owner_receipt(action['receipt'])['url'])
    batch['execution_report'] = fields
    session.save(state)
    return dict(fields, batch=batch['id'])
