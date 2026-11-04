"""Persistent observational usage, separate from workflow state and resource limits."""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import datetime
import re
from pathlib import Path
import time
import uuid

from .common import ApiError, locked, now, read_json
from .events import emit
from .storage import write_json
from . import usage_quota

_current = ContextVar('model_usage', default=None)
FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_creation_input_tokens', 'output_tokens')


def _warning(message):
    try:
        emit('progress', message='Warning: Usage reporting ' + message)
    except (OSError, ValueError):
        pass


def _count(value):
    return type(value) is int and value >= 0


def tokens(value, *, provider='codex', model=False):
    """Normalize provider counters without adding cache or reasoning twice."""
    if not isinstance(value, dict):
        return None
    def get(snake, camel):
        return value.get(camel if model else snake, value.get(snake))
    incoming, outgoing = get('input_tokens', 'inputTokens'), get('output_tokens', 'outputTokens')
    if not _count(incoming) or not _count(outgoing):
        return None
    cache = get('cache_read_input_tokens', 'cacheReadInputTokens') if provider == 'claude' else value.get('cached_input_tokens', 0)
    creation = get('cache_creation_input_tokens', 'cacheCreationInputTokens') if provider == 'claude' else 0
    cache, creation = 0 if cache is None else cache, 0 if creation is None else creation
    if not _count(cache) or not _count(creation):
        return None
    result = dict(input_tokens=incoming + (cache + creation if provider == 'claude' else 0),
                  cached_input_tokens=cache, cache_creation_input_tokens=creation, output_tokens=outgoing)
    reasoning = value.get('reasoning_output_tokens', (value.get('output_tokens_details') or {}).get('thinking_tokens'))
    if _count(reasoning):
        result['reasoning_output_tokens'] = reasoning  # A subset of output; never add to total_tokens.
    result['total_tokens'] = result['input_tokens'] + outgoing
    return result


def _sum(values):
    values = list(values)
    result = {field: sum(v.get(field, 0) for v in values) for field in (*FIELDS, 'total_tokens')}
    if any('reasoning_output_tokens' in v for v in values):
        result['reasoning_output_tokens'] = sum(v.get('reasoning_output_tokens', 0) for v in values)
    return result


def _delta(after, before):
    if any(after.get(k, 0) < before.get(k, 0) for k in FIELDS):
        return None
    return {key: value - before.get(key, 0) for key, value in after.items()}


def choice(role, configuration):
    return dict(role=role, **{key: configuration[key] for key in ('provider', 'model', 'effort')})


def owner_root(session, state):
    """Only an explicit saved parent links jobs; sharing a PR is insufficient."""
    parent = state.get('usage_root')
    if isinstance(parent, str) and Path(parent).is_absolute():
        return Path(parent)
    return session.path


def _empty(legacy=False):
    return {'version': 1, 'tracked_since': now(), 'historical_usage_unknown': legacy,
            'invocations': {}, 'calls': {}, 'sessions': {}}


class Ledger:
    def __init__(self, root, legacy=False):
        self.root = Path(root)
        self.file = self.root / 'usage' / 'ledger.json'
        self.invocation = uuid.uuid4().hex
        self.data = _empty(legacy)
        self.failed = False
        self.changed = False
        self.started = now()
        self._update()

    def _update(self, call=None, session=None, status=None):
        if self.failed:
            if call is not None:
                self.data['calls'][call['id']] = copy.deepcopy(call)
            return
        try:
            with locked(self.file.with_suffix('.lock')):
                data = read_json(self.file, None)
                if data is None:
                    data = copy.deepcopy(self.data)
                if (not isinstance(data, dict) or data.get('version') != 1 or
                        any(not isinstance(data.get(k), dict) for k in ('invocations', 'calls', 'sessions'))):
                    raise ValueError('Unsupported or damaged usage ledger')
                # An unfinished invocation is historical only when its process is gone.
                # Never rewrite another active process's records.
                from .operations import process_start
                for old in data['invocations'].values():
                    if old.get('id') != self.invocation and (old.get('status') != 'running' or
                            not old.get('pid') or process_start(old['pid']) != old.get('process_start')):
                        if old.get('status') == 'running':
                            old['status'] = 'interrupted'
                        for record in data['calls'].values():
                            if record['invocation'] == old['id'] and record['status'] in ('running', 'pending'):
                                record['launch_unknown'] = record['status'] == 'pending'
                                record['status'] = 'interrupted'
                                record['coverage'] = 'partial'
                if call is not None:
                    data['calls'][call['id']] = copy.deepcopy(call)
                if session:
                    data['sessions'][session[0]] = session[1]
                if status is not None:
                    data['invocations'][self.invocation] = self.invocation_record(status)
                self.data = data
                write_json(self.file, data)
        except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            self.failed = True
            self.data['storage_incomplete'] = True
            if call is not None:
                self.data['calls'][call['id']] = copy.deepcopy(call)
            _warning('storage unavailable; totals may be incomplete. Workflow continues.')

    def invocation_record(self, status='running'):
        from .operations import process_identity
        try:
            identity = process_identity()
        except ApiError:
            identity = {}
        return dict(id=self.invocation, started_at=self.started, status=status,
                    pid=identity.get('pid'), process_start=identity.get('start'))

    def save(self, call=None, session=None, status='running'):
        self._update(call, session, status)

    def read_quota(self, provider, env):
        try:
            return dict(usage_quota.read(provider, env, timeout=5), observed_at=now())
        except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return {'status': 'unavailable', 'reason': 'Telemetry read failed', 'observed_at': now(), 'meters': []}


class Call:
    def __init__(self, ledger, metadata, env):
        self.ledger, self.env = ledger, env
        self.record = dict(id=uuid.uuid4().hex, invocation=ledger.invocation, **metadata,
                           status='pending', started_at=now(), coverage='unavailable', usage=None,
                           session_id=None, model_usage={}, duration_seconds=0)
        self.steps, self.turns = {}, {}
        self.models = None
        self.baselines = copy.deepcopy(ledger.data['sessions'])
        self.started = None
        ledger.changed = True
        ledger.save(self.record)
        self.record['quota_before'] = ledger.read_quota(metadata['provider'], env)
        if self.record['quota_before'].get('provider_version'):
            self.record['provider_version'] = self.record['quota_before']['provider_version']
        ledger.save(self.record)

    def launched(self):
        self.started = time.monotonic()
        self.record['status'] = 'running'
        self.ledger.save(self.record)

    def event(self, event):
        kind, provider = event.get('type'), self.record['provider']
        if kind == 'thread.started' or kind == 'system' and event.get('subtype') == 'init':
            self.record['session_id'] = event.get('thread_id') or event.get('session_id')
        elif event.get('session_id') and not self.record['session_id']:
            self.record['session_id'] = event['session_id']
        if isinstance(event.get('claude_code_version'), str):
            self.record['provider_version'] = event['claude_code_version']
        changed = False
        if provider == 'codex' and kind == 'turn.completed':
            value = tokens(event.get('usage'))
            if value:
                # exec emits one completion per turn; explicit IDs also make replay idempotent.
                key = event.get('turn_id') or event.get('id') or str(len(self.turns))
                self.turns[key] = value
                self.record['usage'] = _sum(self.turns.values())
                self.record['coverage'] = 'reported'
                changed = True
        elif provider == 'claude':
            if kind == 'assistant' and isinstance(event.get('message'), dict):
                message = event['message']
                value = tokens(message.get('usage'), provider='claude')
                if message.get('id') and value:
                    self.steps[message['id']] = value
                    if self.models is None and not self.turns:
                        self.record['usage'] = _sum(self.steps.values())
                        self.record['coverage'] = 'partial'
                        changed = True
            if kind == 'result':
                reported = event.get('modelUsage')
                if isinstance(reported, dict) and reported:
                    models = {name: tokens(value, provider='claude', model=True) for name, value in reported.items()}
                    if all(models.values()):
                        self.models = models
                        version = re.match(r'(\d+)\.(\d+)\.(\d+)', self.record.get('provider_version', ''))
                        cumulative = tuple(map(int, version.groups())) >= (2, 1, 277) if version else None
                        baseline = self.baselines.get('claude:' + str(self.record['session_id']), {}) if cumulative is not False else {}
                        deltas = {name: _delta(value, baseline.get(name, {})) for name, value in models.items()}
                        if all(value is not None for value in deltas.values()):
                            self.record['model_usage'] = deltas
                            self.record['usage'] = _sum(deltas.values())
                            # Existing resumed sessions have unknown restored spend, never count it as new.
                            self.record['coverage'] = 'partial' if baseline.get('_incomplete') or self.record.get('resumed') and cumulative is None else 'reported'
                            if baseline.get('_incomplete') or cumulative is not False and self.record.get('resumed') and not baseline:
                                self.record['coverage'] = 'partial'
                                self.record['usage'] = None
                                self.record['model_usage'] = {}
                        else:
                            self.record['coverage'] = 'partial'
                            self.record['usage'] = None
                        changed = True
                if self.models is None:
                    value = tokens(event.get('usage'), provider='claude')
                    if value:
                        key = event.get('uuid') or event.get('result_index') or str(len(self.turns))
                        self.turns[key] = value
                        self.record['usage'] = _sum(self.turns.values())
                        self.record['coverage'] = 'partial'  # Main loop only; delegated usage is absent.
                        changed = True
        if changed or kind in ('thread.started', 'system'):
            session = ('claude:' + str(self.record['session_id']), self.models) if self.models and self.record['session_id'] else None
            self.ledger.save(self.record, session)

    def finish(self, result=None):
        if self.started is None:
            self.record.update(status='not_launched', coverage='unavailable')
        else:
            self.record.update(duration_seconds=max(0, time.monotonic() - self.started),
                               finished_at=now(), status='completed' if result and not result.returncode else
                               'failed' if result else 'interrupted')
            if not result or result.returncode or self.record.get('observation_incomplete'):
                if self.record['usage']:
                    self.record['coverage'] = 'partial'
            if result:
                self.record['exit_code'] = result.returncode
            from .processes import _cancellation
            cancellation = _cancellation.get()
            if cancellation and cancellation.signum:
                self.record['quota_after'] = {'status': 'unavailable', 'reason': 'Cancelled', 'meters': []}
            else:
                self.record['quota_after'] = self.ledger.read_quota(self.record['provider'], self.env)
        session = ('claude:' + str(self.record['session_id']), self.models) if self.models and self.record['session_id'] else None
        if not session and self.record['provider'] == 'claude' and self.record['session_id'] and (not result or result.returncode):
            session = ('claude:' + str(self.record['session_id']), {'_incomplete': True})
        self.ledger.save(self.record, session)
        if self.started is not None:
            usage = self.record['usage']
            count = str(usage['total_tokens']) if usage else 'unavailable'
            overall = [m for m in _meter_changes(self.record.get('quota_before', {}), self.record.get('quota_after', {})) if m['kind'] == 'weekly_all']
            points = ', '.join(_amount(m['consumed_points']) for m in overall) or 'unavailable'
            _progress(f"Usage [{self.record['role']}]: {self.record['provider']} / {self.record['model']} "
                      f"({self.record['effort']}), tokens {count}; weekly quota {points} pp observed; {self.record['coverage']}")


def _progress(message):
    try:
        emit('progress', message=message)
    except (OSError, ValueError):
        pass


def begin_call(metadata, env):
    ledger = _current.get()
    if ledger is None or metadata is None:
        return None
    try:
        return Call(ledger, metadata, env)
    except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _warning('could not start call accounting; workflow continues.')
        return None


def observe(call, method, *args):
    if call is not None:
        try:
            getattr(call, method)(*args)
        except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            call.record['observation_incomplete'] = True
            call.ledger.data['storage_incomplete'] = True
            _warning('could not record a usage observation; workflow continues.')


@contextmanager
def operation(root, *, legacy=True):
    """Nested issue/Owner/reviewer work belongs to the outer job, never double counts."""
    if _current.get() is not None:
        yield _current.get()
        return
    ledger = Ledger(root, legacy)
    token = _current.set(ledger)
    status = 'completed'
    try:
        yield ledger
    except BaseException as exc:
        status = 'interrupted' if isinstance(exc, KeyboardInterrupt) or getattr(exc, 'code', None) == 'cancelled' else 'failed'
        if isinstance(exc, ApiError):
            exc.details['usage'] = summary(ledger.root)
        else:
            try:
                exc.usage = summary(ledger.root)
            except (AttributeError, TypeError):
                pass
        raise
    finally:
        try:
            if ledger.changed:
                ledger.save(status=status)
            render(report(ledger.data))
        except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            _warning('could not render final totals; workflow outcome is unchanged.')
        finally:
            _current.reset(token)


def summary(root):
    current = _current.get()
    try:
        if current and Path(root).resolve() == current.root.resolve():
            return report(current.data)
        data = read_json(Path(root) / 'usage' / 'ledger.json')
        if isinstance(data, dict) and data.get('version') == 1:
            return report(data)
    except (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {'version': 1, 'coverage': 'unavailable', 'reason': 'Usage ledger unavailable', 'roles': [], 'quota': []}
    return {'version': 1, 'coverage': 'unavailable', 'reason': 'Usage tracking has not started', 'roles': [], 'quota': []}


def _meter_changes(before, after):
    previous = {m['id']: m for m in before.get('meters', [])}
    result = []
    for meter in after.get('meters', []):
        old = previous.get(meter['id'])
        valid = old is not None and abs(meter['reset'] - old['reset']) <= 60 and meter['used'] >= old['used']
        result.append(dict(meter, consumed_points=meter['used'] - old['used'] if valid else None,
                           coverage='reported' if valid else 'partial'))
    for key, meter in previous.items():
        if not any(m['id'] == key for m in result):
            result.append(dict(meter, consumed_points=None, coverage='partial'))
    return result


def report(data):
    calls = [c for c in data.get('calls', {}).values()
             if c.get('status') not in ('not_launched', 'pending') and not c.get('launch_unknown')]
    unknown_launches = sum(bool(c.get('launch_unknown')) for c in data.get('calls', {}).values())
    roles, pools, segments = {}, {}, {}
    for call in calls:
        key = tuple(call[k] for k in ('role', 'provider', 'model', 'effort'))
        row = roles.setdefault(key, dict(zip(('role', 'provider', 'model', 'effort'), key), calls=0,
                                        calls_reported=0, calls_partial=0, calls_unavailable=0,
                                        duration_seconds=0, usage=_sum([]), quota={}, model_usage={}, quota_missing_calls=0))
        row['calls'] += 1
        coverage = call.get('coverage', 'unavailable')
        row['calls_' + ('reported' if coverage == 'reported' else 'partial' if call.get('usage') else 'unavailable')] += 1
        row['duration_seconds'] += call.get('duration_seconds', 0)
        if call.get('usage'):
            row['usage'] = _sum([row['usage'], call['usage']])
        for model, value in call.get('model_usage', {}).items():
            row['model_usage'][model] = _sum([row['model_usage'].get(model, _sum([])), value])
        before, after = call.get('quota_before', {}), call.get('quota_after', {})
        changes = _meter_changes(before, after)
        if not changes or any(m['consumed_points'] is None for m in changes):
            row['quota_missing_calls'] += 1
        for meter in changes:
            item = row['quota'].setdefault(meter['id'], {'id': meter['id'], 'kind': meter['kind'], 'consumed_points': 0, 'intervals_reported': 0, 'intervals_missing': 0})
            if meter['consumed_points'] is None:
                item['intervals_missing'] += 1
            else:
                item['intervals_reported'] += 1
                item['consumed_points'] += meter['consumed_points']
        # Separate invocation + reset-window segments. No subtraction across user feedback waits.
        for reading in (before, after):
            for meter in reading.get('meters', []):
                identity = (call['provider'], meter['id'])
                pool = pools.setdefault(identity, dict(provider=call['provider'], **meter, consumed_points=0,
                                                       intervals_reported=0, intervals_missing=0))
                pool.update(used=meter['used'], reset=meter['reset'])
                segment_key = (*identity, call['invocation'])
                old = segments.get(segment_key)
                if old is None:
                    segments[segment_key] = dict(meter)
                elif abs(meter['reset'] - old['reset']) > 60:
                    pool['intervals_missing'] += 1
                    segments[segment_key] = dict(meter)
                elif meter['used'] < old['used']:
                    pool['intervals_missing'] += 1
                else:
                    pool['consumed_points'] += meter['used'] - old['used']
                    pool['intervals_reported'] += 1
                    segments[segment_key] = dict(meter)
    for row in roles.values():
        row['token_coverage'] = 'reported' if row['calls_reported'] == row['calls'] else 'partial' if row['calls_reported'] + row['calls_partial'] else 'unavailable'
        row['quota_coverage'] = 'reported' if not row['quota_missing_calls'] else 'partial' if row['quota'] else 'unavailable'
        row['coverage'] = 'reported' if row['token_coverage'] == row['quota_coverage'] == 'reported' else 'partial' if row['usage']['total_tokens'] or row['quota'] or row['calls_reported'] + row['calls_partial'] else 'unavailable'
        if not row['calls_reported'] + row['calls_partial']:
            row['usage'] = None
        for item in row['quota'].values():
            if not item['intervals_reported']:
                item['consumed_points'] = None
        row['quota'] = list(row['quota'].values())
    for (provider, meter_id), pool in pools.items():
        pool['intervals_missing'] += sum(1 for call in calls if call['provider'] == provider and (
            not any(m['id'] == meter_id for m in call.get('quota_before', {}).get('meters', [])) or
            not any(m['id'] == meter_id for m in call.get('quota_after', {}).get('meters', []))))
        pool['remaining_percentage'] = 100 - pool['used']
        pool['coverage'] = 'partial' if pool['intervals_missing'] else 'reported' if pool['intervals_reported'] else 'unavailable'
        if not pool['intervals_reported']:
            pool['consumed_points'] = None
    for provider in sorted({c['provider'] for c in calls} - {key[0] for key in pools}):
        pools[(provider, '')] = {'provider': provider, 'label': 'OpenAI weekly' if provider == 'codex' else 'Anthropic weekly',
                                'coverage': 'unavailable', 'consumed_points': None, 'remaining_percentage': None, 'reset': None}
    for (provider, meter_id), pool in pools.items():
        attributed = sum(m['consumed_points'] or 0 for row in roles.values() if row['provider'] == provider
                         for m in row['quota'] if m['id'] == meter_id)
        remainder = pool['consumed_points'] - attributed if pool['consumed_points'] is not None else None
        pool['unattributed_points'] = remainder if remainder is not None and remainder >= 0 else None
    available = sum(row['calls_reported'] for row in roles.values())
    return {'version': 1, 'tracked_since': data.get('tracked_since'),
            'historical_usage_unknown': data.get('historical_usage_unknown', False),
            'storage_incomplete': data.get('storage_incomplete', False), 'calls': len(calls), 'calls_launch_unknown': unknown_launches,
            'token_coverage': 'reported' if calls and available == len(calls) else 'partial' if any(c.get('usage') for c in calls) else 'unavailable',
            'coverage': 'reported' if calls and all(r['coverage'] == 'reported' for r in roles.values()) and all(p['coverage'] == 'reported' for p in pools.values()) and not unknown_launches and not data.get('historical_usage_unknown') and not data.get('storage_incomplete') else 'partial' if calls else 'unavailable',
            'roles': list(roles.values()), 'quota': list(pools.values()),
            'attribution': 'Account-wide observations during active invocations; concurrent activity, rounding and delayed updates affect attribution.'}


def _table(headers, rows):
    rows = [[str(cell) for cell in row] for row in [headers, *rows]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    return '\n'.join(' | '.join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)


def _amount(value):
    return 'unavailable' if value is None else f'{value:g}'


def render(value):
    _progress(f"Job usage: {value.get('calls', 0)} calls; tracked since {value.get('tracked_since', 'unknown')}"
              + ('; earlier usage unknown' if value.get('historical_usage_unknown') else '')
              + (f"; {value['calls_launch_unknown']} launch statuses unknown" if value.get('calls_launch_unknown') else ''))
    rows = []
    for pool in value['quota']:
        reset = datetime.datetime.fromtimestamp(pool['reset'], datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if pool.get('reset') else 'unknown'
        rows.append([pool['label'], _amount(pool['consumed_points']), _amount(pool['remaining_percentage']), reset, pool['coverage']])
    _progress(_table(['Provider / meter', 'Consumed (pp)', 'Remaining (%)', 'Resets at', 'Coverage'], rows))
    rows = []
    for role in value['roles']:
        overall = [m for m in role['quota'] if m['kind'] == 'weekly_all']
        consumption = ', '.join(_amount(m['consumed_points']) for m in overall) or 'unavailable'
        rows.append([role['role'], role['provider'], role['model'], role['effort'], role['calls'],
                     role['usage']['total_tokens'] if role['usage'] else 'unavailable', consumption, role['coverage']])
    _progress(_table(['Role', 'Provider', 'Model', 'Effort', 'Calls', 'Reported tokens', 'Weekly (pp)', 'Coverage'], rows))
    _progress(value['attribution'] + ' Role estimates may not sum to provider totals; different meters are not additive.')
