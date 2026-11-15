"""Optional observational execution journal; failures never govern workflow decisions."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import time
import uuid
from datetime import datetime
from .common import ApiError, atomic_json, now, read_json
from .events import emit

_current = ContextVar('session_trace', default=None)


def save(context):
    try:
        atomic_json(context['path'], context['data'])
    except (ApiError, OSError, ValueError, TypeError):
        emit('progress', message='Session trace could not be saved; report coverage is incomplete.')


@contextmanager
def operation(root, enabled):
    if not enabled:
        yield
        return
    path = Path(root) / 'execution-trace.json'
    try:
        data = read_json(path, {'version': 1, 'invocations': [], 'executions': []})
        if not isinstance(data, dict) or data.get('version') != 1 or not all(isinstance(data.get(k), list) for k in ('invocations', 'executions')):
            raise ValueError('Unsupported trace version')
    except (ApiError, OSError, ValueError):
        emit('progress', message='Session trace unavailable; existing history is preserved.')
        yield
        return
    waiting = data.pop('waiting_since', None)
    if waiting:
        try:
            data['waiting_seconds'] = data.get('waiting_seconds', 0) + max(0, (datetime.fromisoformat(now()) - datetime.fromisoformat(waiting)).total_seconds())
        except (ValueError, TypeError):
            data['waiting_coverage'] = 'partial'
    entry = {'id': uuid.uuid4().hex, 'started_at': now(), 'status': 'running'}
    data['invocations'].append(entry)
    context = {'path': path, 'data': data, 'invocation': entry['id']}
    token = _current.set(context)
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
        _current.reset(token)


def begin(stage, accounting=None, output=None, reused=False):
    context = _current.get()
    if context is None or not stage:
        return None
    previous = context['data']['executions']
    record = dict(id=uuid.uuid4().hex, sequence=len(previous)+1, role=stage,
                  invocation=context['invocation'], predecessor=previous[-1]['id'] if previous else None,
                  started_at=now(), status='reused' if reused else 'running', seconds=0,
                  call_id=accounting.record['id'] if accounting else None,
                  output=str(output) if output else None)
    previous.append(record)
    save(context)
    return context, record, time.monotonic()


def end(handle, result):
    if handle is None:
        return
    context, record, started = handle
    record.update(seconds=time.monotonic()-started, finished_at=now(),
                  status='completed' if result and result.returncode == 0 else 'failed' if result else 'interrupted')
    save(context)


def reuse(stage):
    begin(stage, reused=True)
