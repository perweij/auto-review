"""Offline, observational issue-session reports. Never execute session artifacts."""
import html
import json
from pathlib import Path
from .common import ApiError, atomic_json
from .storage import atomic_bytes


def category(role):
    if 'synthesis' in role:
        return 'Synthesis'
    if role.startswith('planning'):
        return 'Planning'
    if role == 'fix':
        return 'Implementation'
    if 'reviewer' in role:
        return 'Review'
    return 'Other'


def read(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def render(data, bounded=False):
    rows = data['activities']
    totals = {}
    for row in rows:
        item = totals.setdefault(row['category'], [0, 0])
        item[0] += row['seconds']
        item[1] += row['tokens'] or 0
    token_rows = [r for r in rows if r.get('coverage') != 'not-applicable']
    waiting = data.get('waiting_seconds')
    text = ['## Session execution report', '',
            'Outcome: **' + html.escape(str(data['outcome'])) + '**', '',
            f"Recorded processing: **{sum(r['seconds'] for r in rows) / 60:.1f} minutes**. "
            'User pauses are excluded. Historical gaps and unmeasured overhead are unclassified.', '',
            f"Token observations: {sum(r['tokens'] is not None for r in token_rows)}/{len(token_rows)} activities. "
            'Totals describe recorded activity, not monetary cost.', '',
            'Recorded user waiting: ' + (f'{waiting/60:.1f} minutes' if waiting is not None else 'unknown') + '.',
            f"Repair cycles: {data.get('repairs', 0)}.", '', '```mermaid', 'flowchart TD']
    groups = []
    for row in rows:
        if row['category'] == 'Other':
            continue
        name = ('Planning' if row['role'].startswith('planning') else
                'Acceptance review' if row['category'] in ('Review', 'Synthesis') else row['category'])
        if not groups or groups[-1][0] != name:
            groups.append([name, 0])
        groups[-1][1] += row['seconds']
    if len(groups) > 11:
        groups = groups[:5] + [[f'{len(groups)-10} intervening phases', sum(g[1] for g in groups[5:-5])]] + groups[-5:]
    for i, (name, seconds) in enumerate(groups):
        text.append(f' n{i}["{name}: {seconds / 60:.1f} min"]')
        if i:
            text.append(f' n{i-1} -->|{i}| n{i}')
    text.append(' final["Session: ' + html.escape(str(data['outcome'])).replace('`', '&#96;') + '"]')
    if groups:
        text.append(f' n{len(groups)-1} --> final')
    text += ['```', '', 'Longest recorded activities:', '']
    text += [f"- {html.escape(r['role'])}: {r['seconds']/60:.1f} minutes"
             for r in sorted(rows, key=lambda r: r['seconds'], reverse=True)[:3]]
    text += ['', '<details><summary>Time and token breakdown</summary>', '']
    for index, title in enumerate(('Recorded processing minutes — partial coverage', 'Reported tokens — partial coverage')):
        values = [(name, values[index] / (60 if index == 0 else 1)) for name, values in totals.items() if values[index] > 0]
        if values:
            text += ['```mermaid', 'pie', ' title ' + title]
            text += [f' "{name}" : {value:.3f}' for name, value in values]
            text += ['```', '']
    text += ['| Activity | Minutes | Reported tokens |', '| --- | ---: | ---: |']
    text += [f'| {name} | {values[0]/60:.1f} | {values[1]} |' for name, values in totals.items()]
    text += ['', '</details>', '', '<details><summary>Execution graph</summary>', '', '```mermaid', 'flowchart TD']
    graph_rows = rows[:60] if bounded else rows
    for index, row in enumerate(graph_rows):
        label = html.escape(' / '.join(str(row[k])[:80] for k in ('role', 'provider', 'model', 'effort', 'status'))).replace('`', '&#96;').replace('\n', ' ')
        text.append(f' e{index}["{index+1}: {label} - {row["seconds"]/60:.1f} min"]')
        if index:
            text.append(f' e{index-1} -->|{index}| e{index}')
    if bounded and len(rows) > len(graph_rows):
        text.append(' omitted["Further executions in local report"]')
    text += ['```', '', 'Order follows recorded execution timestamps; historical causal links may be unavailable.', '', '</details>', '', '<details><summary>Execution details</summary>', '',
             '| ID | Role / configuration | Minutes | Tokens | Execution status |', '| --- | --- | ---: | ---: | --- |']
    for i, row in enumerate(rows, 1):
        safe = lambda value: html.escape(str(value)).replace('|', '&#124;').replace('\n', ' ')
        line = f"| {i} | {safe(row['role'])}: {safe(row['provider'])} / {safe(row['model'])} / {safe(row['effort'])} | {row['seconds']/60:.2f} | {row['tokens'] if row['tokens'] is not None else 'unknown'} | {safe(row['status'])} |"
        if bounded and len('\n'.join(text)) + len(line) > 27000:
            text += ['', 'Further execution details omitted; the local report retains all records.']
            break
        text.append(line)
    text += ['', 'Execution status describes the process; acceptance outcomes are reported below.', '', '</details>', '', '<details><summary>Stage outcomes</summary>', '']
    for outcome in data.get('outcomes', []):
        excerpt = html.escape(outcome['summary'][:1200]).replace('`', '&#96;').replace('@', '&#64;')
        # Public summaries must not expose local absolute paths.
        import re
        excerpt = re.sub(r'(?<![\w:])/(?:[^\s/]+/)*[^\s]*', '[local path]', excerpt)
        if bounded and len('\n'.join(text)) + len(excerpt) > 29000:
            text.append('Further outcomes omitted; see the local report.')
            break
        text += ['**' + html.escape(str(outcome['stage'])) + '**', '', excerpt, '']
    text += ['</details>', '']
    return '\n'.join(text)


def generate(directory):
    root = Path(directory).resolve()
    state = read(root / 'session.json', {})
    if state.get('kind') != 'issue':
        # Existing stores identify workflows with a versioned kind.
        if 'issue' not in state or 'assessment' not in state:
            raise ValueError('Expected an existing issue-session directory.')
    ledger = read(root / 'usage/ledger.json', {})
    activities = []
    for call in ledger.get('calls', {}).values():
        activities.append(dict(id=call['id'], role=call.get('role', 'unknown'),
            category=category(call.get('role', '')), provider=call.get('provider', 'unknown'),
            model=call.get('model', 'unknown'), effort=call.get('effort', 'unknown'),
            status=call.get('status', 'unknown'), started_at=call.get('started_at', ''),
            invocation=call.get('invocation'), finished_at=call.get('finished_at'),
            seconds=max(0, call.get('duration_seconds', 0)),
            tokens=(call.get('usage') or {}).get('total_tokens'), coverage=call.get('coverage', 'unavailable')))
    trace = read(root / 'execution-trace.json', {})
    by_call = {r['id']: r for r in activities}
    for event in trace.get('executions', []):
        row = by_call.get(event.get('call_id'))
        if row is not None:
            row.update(trace_id=event['id'], trace_invocation=event['invocation'], sequence=event['sequence'],
                       predecessor=event.get('predecessor'))
        elif event.get('status') == 'reused':
            activities.append(dict(id=event['id'], role=event['role'], category=category(event['role']),
                provider='original configuration', model='retained', effort='retained', status='reused',
                started_at=event['started_at'], sequence=event['sequence'], seconds=0, tokens=0, coverage='not-applicable'))
    owner = read(Path(state['owner']) / 'session.json', {}) if state.get('owner') else {}
    for batch in owner.get('batches', []):
        for verification in batch.get('verification_runs', []):
            source = verification.get('input', {})
            # Crash recovery spends a timeout allowance, which is not measured duration.
            measured = verification.get('status') not in ('running', 'interrupted')
            activities.append(dict(id='verification-' + str(len(activities)), role='Standalone verification',
                category='Verification', provider='controller', model='none', effort='none',
                status=verification.get('status', 'unknown'), started_at=source.get('started_at', ''),
                seconds=verification.get('elapsed_seconds', 0) if measured else 0,
                tokens=None, coverage='not-applicable'))
    for invocation in trace.get('invocations', []):
        if 'seconds' not in invocation:
            continue
        # Sequential workflow: subtract measured child work within this invocation.
        children = [r for r in activities if r.get('trace_invocation') == invocation['id'] or
                    r['category'] == 'Verification' and invocation['started_at'] <= r['started_at'] <= invocation.get('finished_at', '')]
        overhead = max(0, invocation['seconds'] - sum(r['seconds'] for r in children))
        if overhead > 0:
            activities.append(dict(id='overhead-' + invocation['id'], role='Orchestration overhead', category='Other',
                provider='controller', model='none', effort='none', status=invocation['status'],
                started_at=invocation.get('finished_at', ''), seconds=overhead, tokens=None, coverage='not-applicable'))
    # Stable sort preserves ledger insertion order for historical timestamp ties.
    activities.sort(key=lambda row: (row['started_at'], row.get('sequence', 0)))
    data = dict(version=1, outcome=state.get('phase', 'unknown'), activities=activities,
                coverage='partial', waiting_seconds=trace.get('waiting_seconds'), trace_version=trace.get('version'),
                repairs=sum(len(b.get('review_history', {}).get('cycles', [])) for b in owner.get('batches', [])))
    data['outcomes'] = []
    assessment = state.get('assessment') or {}
    if assessment.get('summary'):
        data['outcomes'].append({'stage': 'Planning', 'summary': assessment['summary']})
    for batch in owner.get('batches', []):
        for attempt in batch.get('attempts', []):
            result = attempt.get('result')
            if isinstance(result, dict) and isinstance(result.get('summary'), str):
                data['outcomes'].append({'stage': attempt.get('role', 'Implementation'), 'summary': result['summary']})
        review = batch.get('review') or {}
        if isinstance(review, dict) and isinstance(review.get('summary'), str):
            data['outcomes'].append({'stage': 'Acceptance', 'summary': review['summary']})
    output = root / 'reports'
    output.mkdir(exist_ok=True, mode=0o700)
    atomic_json(output / 'session.json', data)
    path = output / 'session.md'
    atomic_bytes(path, render(data).encode())
    return {'report_path': str(path), 'report_data_path': str(output / 'session.json')}, render(data, bounded=True)


def finish(flow, result):
    if not flow.state.get('session_report'):
        return result
    try:
        trace_path = Path(flow.path) / 'execution-trace.json'
        trace = read(trace_path, {})
        if trace and flow.state.get('phase') not in ('ready', 'complete'):
            from .common import now
            trace.setdefault('waiting_since', now())
            atomic_json(trace_path, trace)
        fields, body = generate(flow.path)
        result.update(fields)
        if flow.state.get('phase') in ('ready', 'complete') and result.get('exit_code', 0) == 0:
            from .common import digest
            key = 'session-report:' + digest([flow.state.get('plan'), flow.state.get('remote_head')])
            action = flow.state.get('actions', {}).get(key)
            if action:
                body = action.get('source_body', action['body'])
            flow.writes.comment(body, key)
            result['report_publication'] = 'complete'
        else:
            result['report_publication'] = 'local-only'
    except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        result['report_warning'] = str(exc)
        result['report_publication'] = 'incomplete'
    return result
