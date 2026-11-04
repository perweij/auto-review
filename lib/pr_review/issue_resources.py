"""Persistent active-runtime accounting, independent of model token pricing."""
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from .common import ApiError, now
from .events import emit
from . import issue_quota

_current = ContextVar('issue_resources', default=None)


def duration(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([smh])', str(value))
    if not match:
        raise ApiError('invalid_request', 'max-runtime requires a positive duration such as 30m or 2h.')
    seconds = float(match[1]) * {'s': 1, 'm': 60, 'h': 3600}[match[2]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ApiError('invalid_request', 'max-runtime must be positive and finite.')
    return seconds


def duration_text(seconds):
    """Round-trip saved seconds through the decimal-only duration grammar."""
    return format(Decimal(str(seconds)), 'f') + 's'


def validate(request):
    if request.get('max_runtime') is not None:
        duration(request['max_runtime'])
    for key in ('min_weekly_quota_remaining', 'max_weekly_quota_use'):
        value = request.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100):
            raise ApiError('invalid_request', key + ' must be greater than zero and at most 100.')


def apply_limits(record, request):
    """Change limits without refunding active runtime or cumulative quota use."""
    validate(request)
    if 'max_runtime' in request:
        if request['max_runtime'] is None:
            record.pop('max_runtime', None)
        else:
            record['max_runtime'] = duration(request['max_runtime'])
    for key in ('min_weekly_quota_remaining', 'max_weekly_quota_use'):
        if key in request and request[key] is None:
            record.pop(key, None)
        elif request.get(key) is not None:
            value = request[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100:
                raise ApiError('invalid_request', key + ' must be greater than zero and at most 100.')
            record[key] = value


@contextmanager
def execution(state, save, request, *, provider_scope=None):
    validate(request)
    record = state.setdefault('resources', {'version': 1, 'active_seconds': 0, 'started_at': now()})
    apply_limits(record, request)
    record['quota_baselines'] = {}
    record.pop('quota_polled', None)
    started = time.monotonic()
    base = record['active_seconds']
    token = _current.set((record, save, started, base))
    try:
        save()
        record["providers"] = providers(state) if provider_scope is None else provider_scope
        check()
        yield
    finally:
        try:
            record['active_seconds'] = base + time.monotonic() - started
            save()
        finally:
            _current.reset(token)


def check():
    current = _current.get()
    if current is None:
        return
    record, save, started, base = current
    elapsed = base + time.monotonic() - started
    record['active_seconds'] = elapsed
    if record.get('max_runtime') is not None and elapsed >= record['max_runtime']:
        raise ApiError('resource_limit', 'Configured active runtime allowance reached.', next_action='increase_runtime')
    quota(record)
    elapsed = base + time.monotonic() - started
    record['active_seconds'] = elapsed
    if elapsed - record.get('last_checkpoint_seconds', -10) >= 10:
        record['last_checkpoint_seconds'] = elapsed
        save()
        emit('progress', message=f'Autonomous execution: {elapsed:.0f}s active elapsed')
    if record.get('max_runtime') is not None and elapsed >= record['max_runtime']:
        raise ApiError('resource_limit', 'Configured active runtime allowance reached.', next_action='increase_runtime')


def providers(state):
    configuration = state.get('configuration', {})
    # Named references are provenance. API edits and attached Owner settings
    # determine the roles that will actually execute.
    found = {configuration.get('planning', configuration.get('processing', {})).get(
        'provider', configuration.get('processing_provider', 'claude'))}
    owner = state.get('owner_settings', {})
    implementation = owner.get('fix', configuration.get('fix', {}))
    if implementation.get('provider'):
        found.add(implementation['provider'])
    final = owner.get('final_review')
    if final:
        found.update(s['provider'] for s in final['reviewer_slots'])
        found.add(final['synthesis']['provider'])
    elif owner.get('fix') and owner.get('review'):
        # Legacy Owner settings derive acceptance from the task-check choice.
        found.add(owner['review']['provider'])
    else:
        found.update(s['provider'] for s in configuration.get('reviewer_slots', []))
        if configuration.get('processing'):
            found.add(configuration['processing']['provider'])
    return sorted(found)


def quota(record):
    if not any(k in record for k in ('min_weekly_quota_remaining', 'max_weekly_quota_use')):
        return
    tick = time.monotonic()
    if tick - record.get('quota_polled', -float('inf')) < 60:
        return
    record['quota_polled'] = tick
    record['quota_observed_at'] = now()
    missing = []
    for provider in record.get('providers', []):
        remaining = max(0, record.get('max_runtime', float('inf')) - record.get('active_seconds', 0))
        windows = issue_quota.read_codex(timeout=min(10, remaining)) if provider == 'codex' else None
        if not windows:
            missing.append(provider)
            continue
        for pool, reading in windows.items():
            key = provider + ':' + pool
            old = record['quota_baselines'].get(key)
            record['quota_baselines'][key] = reading
            if old and old['reset'] != reading['reset']:
                record.setdefault('quota_unknown', []).append(key + ' reset gap')
                missing.append(key + ' reset gap')
            elif old:
                totals = record.setdefault('quota_consumed', {})
                totals[key] = totals.get(key, 0) + max(0, reading['used'] - old['used'])
            remaining = 100 - reading['used']
            emit('progress', message=f'{key}: weekly quota {remaining:g}% remaining (account-wide observation)')
            if ('min_weekly_quota_remaining' in record and remaining <= record['min_weekly_quota_remaining']
                    or 'max_weekly_quota_use' in record and record.get('quota_consumed', {}).get(key, 0) >= record['max_weekly_quota_use']):
                raise ApiError('resource_limit', 'Observed weekly quota threshold reached.', provider=provider)
    if 'max_weekly_quota_use' in record:
        missing += record.get('quota_unknown', [])
    record['quota_unavailable'] = sorted(set(missing))
    if missing:
        if not record.get('max_runtime'):
            raise ApiError('resource_limit', 'Weekly quota monitoring unavailable; configure --max-runtime for fallback.', providers=missing)
        emit('progress', message='Weekly quota monitoring incomplete; using configured runtime fallback for ' + ', '.join(missing))


def active():
    return _current.get() is not None
