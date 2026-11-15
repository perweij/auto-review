"""Versioned observational stage journal; never authority for workflow decisions."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
import time
import uuid
import copy

from .common import ApiError, atomic_json, now, read_json, digest
from .events import emit
from .storage import hash_file

_current = ContextVar('session_trace', default=None)
_active = ContextVar('session_trace_stage', default=None)


def warn():
    emit('progress', message='Session trace unavailable or incomplete; workflow continues.')


def initialize(root, identity):
    path = Path(root) / 'execution-trace.json'
    if path.exists():
        return
    try:
        atomic_json(path, dict(version=2, session_id=identity, invocations=[], stages=[],
                               transitions=[], executions=[], waiting_seconds=0, coverage='complete'))
    except (ApiError, OSError, ValueError, TypeError):
        warn()


def validate(data, identity=None):
    if (not isinstance(data, dict) or data.get('version') != 2
            or identity is not None and data.get('session_id') != identity
            or not all(isinstance(data.get(k), list) for k in ('invocations', 'stages', 'transitions', 'executions'))):
        raise ValueError('Reporting requires an intact version-2 trace from a new issue session.')
    if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for field in ('invocations', 'stages', 'executions') for r in data[field]):
        raise ValueError('Invalid trace records.')
    if any(not isinstance(s.get('choice'), dict) or not isinstance(s.get('inputs'), list) or not isinstance(s.get('call_ids'), list) or
           not isinstance(s.get('sequence'), int) or not isinstance(s.get('context'), dict) or
           any(not isinstance(s.get(k), str) for k in ('role', 'status', 'outcome', 'invocation', 'key'))
           for s in data['stages']):
        raise ValueError('Invalid stage record.')
    ids = [s.get('id') for s in data['stages'] if isinstance(s, dict)]
    if len(ids) != len(data['stages']) or None in ids or len(ids) != len(set(ids)):
        raise ValueError('Invalid stage identities in session trace.')
    if any(not isinstance(e, dict) or e.get('source') not in ids or e.get('target') not in ids for e in data['transitions']):
        raise ValueError('Invalid causal reference in session trace.')
    return data


def save(context):
    try:
        atomic_json(context['path'], context['data'])
    except (ApiError, OSError, ValueError, TypeError):
        context['data']['coverage'] = 'partial'
        warn()


@contextmanager
def operation(root, enabled, identity=None):
    if not enabled:
        yield
        return
    path = Path(root) / 'execution-trace.json'
    try:
        data = validate(read_json(path), identity)
    except (ApiError, OSError, ValueError, TypeError):
        warn()
        yield
        return
    # A process died without finalizing these observations. Never infer success.
    for record in data['invocations'] + data['stages'] + data['executions']:
        if record.get('status') == 'running':
            record['status'] = 'interrupted'
            record['timing_coverage'] = 'unknown'
            data['coverage'] = 'partial'
    waiting = data.pop('waiting', None)
    if waiting:
        try:
            data['waiting_seconds'] += max(0, (datetime.fromisoformat(now()) - datetime.fromisoformat(waiting['since'])).total_seconds())
        except (ValueError, KeyError, TypeError):
            data['coverage'] = 'partial'
    entry = dict(id=uuid.uuid4().hex, started_at=now(), status='running')
    data['invocations'].append(entry)
    context = dict(path=path, data=data, invocation=entry['id'], cursor=None)
    token = _current.set(context)
    active_token = _active.set(None)
    started = time.monotonic()
    save(context)
    try:
        yield
        entry['status'] = 'completed'
    except BaseException:
        entry['status'] = 'interrupted'
        raise
    finally:
        entry.update(finished_at=now(), seconds=time.monotonic()-started)
        save(context)
        _active.reset(active_token)
        _current.reset(token)


def reference(artifact):
    """Only exact artifact identities establish a source; no role/time matching."""
    context = _current.get()
    if context is None or artifact is None:
        return None
    try:
        path = str(Path(artifact).resolve())
        checksum = hash_file(Path(path))
    except (OSError, ApiError):
        return None
    return next((s['id'] for s in reversed(context['data']['stages'])
                 if s.get('artifact') == path and s.get('artifact_hash') == checksum
                 and s.get('status') in ('completed', 'reused')), None)


def start(role, *, choice=None, inputs=(), artifact=None, key=None, reason='consumed_result', context=None):
    journal = _current.get()
    if journal is None:
        return None
    stages = journal['data']['stages']
    path = str(Path(artifact).resolve()) if artifact else None
    key = str(key or path or role)
    previous = next((s for s in reversed(stages) if s.get('key') == key and s['role'] == role), None)
    sources = list(dict.fromkeys(s for s in inputs if s))
    if journal.get('cursor') and not sources:
        sources.append(journal['cursor'])
    known = {s['id'] for s in stages}
    if any(source not in known for source in sources):
        journal['data']['coverage'] = 'partial'
        sources = [source for source in sources if source in known]
    record = dict(id=uuid.uuid4().hex, sequence=len(stages)+1, role=role, key=key,
                  invocation=journal['invocation'], choice=copy.deepcopy(choice or {}), inputs=sources,
                  artifact=path, status='running', outcome='running', summary='', started_at=now(),
                  call_ids=[], context=context or {})
    if previous and previous['status'] in ('failed', 'interrupted', 'incomplete'):
        record['retry_of'] = previous['id']
        sources.append(previous['id'])
    stages.append(record)
    for source in dict.fromkeys(sources):
        journal['data']['transitions'].append(dict(source=source, target=record['id'],
            reason='retry' if source == record.get('retry_of') else reason))
    token = _active.set(record['id'])
    handle = [journal, record, time.monotonic(), token]
    save(journal)
    return handle


def complete(handle, value, artifact=None):
    if handle is None:
        return None
    journal, record, started, _ = handle
    value = value if isinstance(value, dict) else {}
    journal.pop('last_failed', None)
    record.update(status='completed', outcome=value.get('assessment', value.get('decision', value.get('status', 'result_available'))),
                  summary=str(value.get('summary', ''))[:4000], finished_at=now(), seconds=time.monotonic()-started)
    path = artifact or record.get('artifact')
    if path:
        try:
            record.update(artifact=str(Path(path).resolve()), artifact_hash=hash_file(Path(path)))
        except (ApiError, OSError):
            journal['data']['coverage'] = 'partial'
            record['artifact_unavailable'] = True
    save(journal)
    return record['id']


def close(handle, error=None):
    if handle is None:
        return
    journal, record, started, token = handle
    if record['status'] == 'running' or error is not None:
        status = 'interrupted' if error is None or getattr(error, 'code', '') == 'cancelled' or isinstance(error, KeyboardInterrupt) else 'failed'
        record.update(status=status, outcome=getattr(error, 'code', status), seconds=time.monotonic()-started, finished_at=now())
        journal['last_failed'] = record['id']
        save(journal)
    _active.reset(token)


@contextmanager
def stage(role, **kwargs):
    handle = start(role, **kwargs)
    try:
        yield handle
    except BaseException as exc:
        close(handle, exc)
        raise
    else:
        close(handle)


def reuse(role, artifact, value, *, choice=None, inputs=()):
    with stage(role, choice=choice, inputs=inputs, artifact=artifact, reason='reused_result') as handle:
        return complete_reuse(handle, artifact, value)


def decision(reason, *, sources=(), summary='', key=None):
    journal = _current.get()
    if journal is None:
        return None
    identity = digest([reason, key, list(sources), summary])
    existing = next((s for s in journal['data']['stages'] if s.get('decision_identity') == identity), None)
    if existing:
        journal['cursor'] = existing['id']
        return existing['id']
    with stage('control/' + reason, inputs=sources, key=key or identity, reason=reason) as handle:
        result = complete(handle, dict(status=reason, summary=summary))
        if handle:
            handle[1]['decision_identity'] = identity
            journal['cursor'] = result
            save(journal)
        return result


def begin(role, accounting=None, output=None, reused=False):
    journal = _current.get()
    if journal is None:
        return None
    executions = journal['data']['executions']
    record = dict(id=uuid.uuid4().hex, sequence=len(executions)+1, role=role,
                  invocation=journal['invocation'], stage_id=_active.get(), status='running',
                  started_at=now(), call_id=accounting.record['id'] if accounting else None)
    executions.append(record)
    if record['stage_id'] and record['call_id']:
        node = next(s for s in journal['data']['stages'] if s['id'] == record['stage_id'])
        node['call_ids'].append(record['call_id'])
    save(journal)
    return journal, record, time.monotonic()


def end(handle, result):
    if handle is None:
        return
    journal, record, started = handle
    record.update(seconds=time.monotonic()-started, finished_at=now(), status='completed' if result and not result.returncode else 'failed' if result else 'interrupted')
    save(journal)


def boundary(root, identity, result):
    """Wait accounting is independent of whether a report is requested."""
    path = Path(root) / 'execution-trace.json'
    try:
        data = validate(read_json(path), identity)
        if result.get('outcome') != 'noop':
            data['outcome'] = result.get('outcome', 'unknown')
        if result.get('outcome') in ('approval', 'questions', 'needs_attention', 'resource_limit', 'blocked_external', 'interrupted', 'automation_failed', 'uncertain'):
            data.setdefault('waiting', dict(since=now(), reason=result.get('next_action', result['outcome'])))
        atomic_json(path, data)
    except (ApiError, OSError, ValueError, TypeError):
        warn()


def complete_reuse(handle, artifact, value):
    if handle is None:
        return None
    journal, record, _, _ = handle
    source = reference(artifact)
    if not source:
        # Recovery is allowed only after the caller validates the exact output
        # of an intent recorded before the interruption.
        path = str(Path(artifact).resolve())
        original = next((s for s in reversed(journal['data']['stages'])
                         if s['id'] != record['id'] and s['role'] == record['role']
                         and (s.get('artifact') == path or s.get('key') == path)
                         and s['status'] == 'interrupted'), None)
        if original:
            try:
                original.update(status='completed', outcome=value.get('assessment', value.get('decision', value.get('status', 'result_available'))),
                                summary=str(value.get('summary', ''))[:4000], artifact=path,
                                artifact_hash=hash_file(Path(path)), recovered_result=True, timing_coverage='unknown')
                source = original['id']
            except (OSError, ApiError):
                pass
    if source:
        original = next(s for s in journal['data']['stages'] if s['id'] == source)
        record['choice'] = original['choice']
        record['inputs'].append(source)
        journal['data']['transitions'].append(dict(source=source, target=record['id'], reason='reused_result'))
    else:
        journal['data']['coverage'] = 'partial'
    complete(handle, value, artifact)
    record.update(status='reused', reused_from=source, seconds=0)
    save(journal)
    return record['id']


def mark(name, identity):
    context = _current.get()
    if context is not None and identity:
        context[name] = identity


def marked(name):
    context = _current.get()
    return context.get(name) if context else None


def restart(handle, reason, summary):
    """Split a validated format failure from its corrected provider attempt."""
    if handle is None:
        return
    journal, record, _, _ = handle
    close(handle, ApiError('invalid_result', summary))
    decision(reason, sources=[record['id']], summary=summary, key=record['id'])
    replacement = start(record['role'], choice=record['choice'], inputs=record['inputs'],
                        artifact=record.get('artifact'), key=record['key'], context=record['context'])
    handle[:] = replacement


def invalidate(identity, error):
    """A caller's final integrity check can invalidate a validated assessment."""
    journal = _current.get()
    if journal is None or not identity:
        return
    record = next((s for s in journal['data']['stages'] if s['id'] == identity), None)
    if record:
        record['validated_outcome'] = record['outcome']
        record.update(status='failed', outcome=getattr(error, 'code', 'integrity_failed'))
        journal['last_failed'] = identity
        save(journal)
