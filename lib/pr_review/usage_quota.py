"""Bounded, read-only subscription readings. Never run a model to obtain usage."""
import datetime
import json
import math
import os
import selectors
import shutil
import subprocess
import tempfile
import time

from .processes import stop_group


def number(value):
    return type(value) in (int, float) and 0 <= value <= 100


def epoch(value):
    try:
        result = float(value) if type(value) in (int, float) else datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) and 0 <= result < 253402300800 else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _records(argv, env, timeout, requests=None):
    """A small bounded transport, deliberately independent of model accounting."""
    child = None
    try:
        with tempfile.TemporaryDirectory(prefix='pr-review-usage-') as cwd:
            child = subprocess.Popen(argv, cwd=cwd, env={**env, 'PR_REVIEW_USAGE_PROBE': '1'}, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            if requests:
                child.stdin.write((json.dumps(requests[0]) + '\n').encode())
                child.stdin.flush()
            else:
                child.stdin.close()
            records, pending, size = [], b'', 0
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                        data = os.read(key.fd, 65536)
                        if not data:
                            return records
                        size += len(data)
                        if size > 2 * 1024 * 1024:
                            return []
                        pending += data
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            try:
                                record = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(record, dict):
                                continue
                            records.append(record)
                            if requests and record.get('id') == 1:
                                if record.get('error'):
                                    return []
                                for request in requests[1:]:
                                    child.stdin.write((json.dumps(request) + '\n').encode())
                                child.stdin.flush()
                            if requests and record.get('id') == 2:
                                return records
            return []
    except (OSError, ValueError, TypeError):
        return []
    finally:
        if child is not None:
            stop_group(child, grace=.1)
            for pipe in (child.stdin, child.stdout):
                if pipe and not pipe.closed:
                    pipe.close()


def read(provider, env, timeout=5):
    """Return meter metadata, or an explicit availability gap; no secrets are retained."""
    env = dict(os.environ if env is None else env)
    api_keys = ('OPENAI_API_KEY', 'CODEX_API_KEY') if provider == 'codex' else ('ANTHROPIC_API_KEY',)
    if any(env.get(key) for key in api_keys):
        return {'status': 'unavailable', 'reason': 'API credentials present; subscription attribution unavailable', 'meters': []}
    executable = shutil.which(provider, path=env.get('PATH'))
    if not executable:
        return {'status': 'unavailable', 'reason': 'Provider executable unavailable', 'meters': []}
    meters, version = [], None
    if provider == 'codex':
        records = _records([executable, 'app-server', '--listen', 'stdio://'], env, timeout, [
            {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'auto-review-usage', 'version': '1'}}},
            {'method': 'initialized'}, {'id': 2, 'method': 'account/rateLimits/read'}])
        for record in records:
            if record.get('id') != 2 or not isinstance(record.get('result'), dict):
                continue
            result = record['result']
            pools = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
            if not isinstance(pools, dict):
                continue
            for pool, limits in pools.items():
                if not isinstance(limits, dict):
                    continue
                for name in ('primary', 'secondary'):
                    window = limits.get(name)
                    if not isinstance(window, dict) or window.get('windowDurationMins') != 10080:
                        continue
                    used, reset = window.get('usedPercent'), epoch(window.get('resetsAt'))
                    if number(used) and reset is not None:
                        meters.append({'id': str(pool) + ':' + name, 'label': 'OpenAI weekly (' + str(pool) + ')',
                                       'kind': 'weekly_all', 'scope': None, 'used': used, 'reset': reset})
    elif provider == 'claude':
        records = _records([executable, '-p', '/usage', '--output-format', 'stream-json', '--verbose',
                            '--no-session-persistence', '--tools', '', '--strict-mcp-config',
                            '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                            '--settings', '{"sandbox":{"enabled":false}}'], env, timeout)
        version = next((r.get('claude_code_version') for r in records if isinstance(r.get('claude_code_version'), str)), None)
        # A local /usage result must report no inference, never accept a model's interpretation.
        results = [r for r in records if r.get('type') == 'result']
        if (not results or results[-1].get('is_error') or results[-1].get('total_cost_usd') != 0 or
                any(results[-1].get('usage', {}).get(key, 0) != 0 for key in
                    ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))):
            records = []
        for record in records:
            report = record.get('usage_report')
            if not isinstance(report, dict) or not isinstance(report.get('rate_limits'), dict):
                continue
            for row in report['rate_limits'].get('limits') or []:
                if not isinstance(row, dict) or row.get('kind') not in ('weekly_all', 'weekly_scoped'):
                    continue
                used, reset, scope = row.get('percent'), epoch(row.get('resets_at')), row.get('scope')
                if not number(used) or reset is None:
                    continue
                # Store only the documented model/surface labels, not arbitrary report data.
                scope = {k: v.get('display_name') for k, v in (scope if isinstance(scope, dict) else {}).items()
                         if k in ('model', 'surface') and isinstance(v, dict) and isinstance(v.get('display_name'), str)}
                label = 'Anthropic weekly, ' + (', '.join(scope.values()) if scope else
                                               'all models' if row['kind'] == 'weekly_all' else 'unspecified scope')
                meters.append({'id': row['kind'] + ':' + json.dumps(scope, sort_keys=True), 'label': label,
                               'kind': row['kind'], 'scope': scope or None, 'used': used, 'reset': reset})
    return {'status': 'reported' if meters else 'unavailable', 'meters': meters, 'provider_version': version,
            **({} if meters else {'reason': 'No supported subscription reading returned'})}
