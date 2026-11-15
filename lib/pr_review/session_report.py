"""Render new-session execution traces without reconstructing workflow history."""
import html
import re
from pathlib import Path

from .common import ApiError, atomic_json, read_json, digest
from .storage import atomic_bytes
from . import session_trace


def category(role):
    if role == 'task-check':
        return 'Task checks'
    if role == 'verification':
        return 'Verification'
    if role in ('implementation', 'fix'):
        return 'Implementation'
    if role in ('acceptance', 'adjudication') or 'synthesis' in role:
        return 'Synthesis'
    if role.startswith('planning'):
        return 'Planning'
    if 'reviewer' in role:
        return 'Review'
    return 'Other'


def safe(value, limit=1200):
    text = str(value)[:limit]
    text = re.sub(r'(?<![\w:/])/(?:[^\s/]+/)*[^\s/]+', '[local path]', text)
    return html.escape(text).replace('`', '&#96;').replace('@', '&#64;').replace('|', '&#124;').replace('\n', ' ')


def graph(data, *, compact, limit=None):
    groups, mapping = {}, {}
    for row in data['activities']:
        if not row.get('stage'):
            continue
        context = row.get('context', {})
        if compact and not row['role'].startswith('control/'):
            kind = ('Planning' if row['role'].startswith('planning') else
                    'Acceptance review' if row['category'] in ('Review', 'Synthesis') else row['category'])
            key = (kind, context.get('batch'), context.get('cycle'), row.get('group_epoch', 0))
            label = kind + (f" · cycle {context['cycle']}" if context.get('cycle') is not None else '')
        else:
            key, label = row['id'], row['role']
        if key not in groups:
            groups[key] = dict(id='n' + str(len(groups)), label=label, seconds=0, outcome='', sequences=[], choices=set())
        group = groups[key]
        group['seconds'] += row['seconds']
        group['outcome'] = row['status']
        group['sequences'].append(str(row['sequence']))
        if row.get('model') not in ('none', 'unknown'):
            group['choices'].add(' / '.join(row[k] for k in ('provider', 'model', 'effort')))
        mapping[row['id']] = group['id']
    selected = list(groups.values())
    if limit and len(selected) > limit:
        hidden = selected[5:-(limit-6)]
        hidden_ids = {g['id'] for g in hidden}
        collapsed = dict(id='omitted', label=f'{len(hidden)} additional groups', seconds=sum(g['seconds'] for g in hidden), outcome='expand local report', sequences=[], choices=set())
        selected = selected[:5] + [collapsed] + selected[-(limit-6):]
        mapping = {key: 'omitted' if value in hidden_ids else value for key, value in mapping.items()}
    lines = ['```mermaid', 'flowchart TD']
    for group in selected:
        numbers = ', '.join(group['sequences'])
        label = f"{numbers}: {group['label']} · {group['seconds']/60:.1f} min · {group['outcome']}"
        if not compact:
            label += ' · ' + ', '.join(sorted(group['choices']))
        lines.append(f' {group["id"]}["{safe(label, 360)}"]')
    edges = set()
    for number, edge in enumerate(data['transitions'], 1):
        source, target = mapping.get(edge['source']), mapping.get(edge['target'])
        signature = source, target, edge['reason']
        if source and target and source != target and signature not in edges:
            edges.add(signature)
            lines.append(f' {source} -->|"{number}: {safe(edge["reason"], 60)}"| {target}')
    if not selected:
        lines.append(' empty["No stage observations available"]')
    return lines + ['```']


def render(data, bounded=False):
    rows = data['activities']
    totals = {}
    for row in rows:
        total = totals.setdefault(row['category'], [0, 0])
        total[0] += row['seconds']
        total[1] += row['tokens'] or 0
    waiting = data.get('waiting_seconds')
    text = ['## Session execution report', '', f"Outcome: **{safe(data['outcome'])}**", '',
            f"Recorded processing: **{sum(r['seconds'] for r in rows)/60:.1f} minutes**. User pauses are excluded.", '',
            f"Trace coverage: {data['coverage']}. Unmeasured work remains unknown.",
            f"Reported tokens: {sum(r['tokens'] or 0 for r in rows) if data['reported_calls'] else 'unknown'}; observations for {data['reported_calls']}/{data['total_calls']} calls. Tokens are not monetary cost.",
            'Recorded user waiting: ' + (f'{waiting/60:.1f} minutes.' if waiting is not None else 'unknown.'),
            f"Repair cycles: {data['repairs']}.", '', *graph(data, compact=True, limit=12), '',
            'Edges show consumed results and controller decisions; execution is sequential. Node numbers show recorded stage order.', '',
            'Longest recorded activities:', '']
    if data.get('owner'):
        owner = data['owner']
        text[2:2] = [f"Owner batch: {safe(owner['batch'])}. PR: {safe(owner.get('url') or 'unknown')}.",
                     f"Candidate tree: {safe(owner['candidate'])}. " +
                     ('Read-only verification.' if owner.get('verify_only') else 'Fixing round.'),
                     'This is a local execution snapshot; it does not establish that changes were pushed or replies published.', '']
    text += [f"- {safe(r['role'])}: {r['seconds']/60:.1f} minutes" for r in sorted(rows, key=lambda r: r['seconds'], reverse=True)[:3]]
    text += ['', '<details><summary>Time and token breakdown</summary>', '']
    for index, label in enumerate(('Recorded processing minutes', 'Reported tokens')):
        values = [(name, value[index] / (60 if index == 0 else 1)) for name, value in totals.items() if value[index] > 0]
        if values:
            partial = data['coverage'] != 'complete' if index == 0 else data['reported_calls'] != data['total_calls'] or data.get('token_partial')
            text += ['```mermaid', 'pie', ' title ' + label + (' - partial coverage' if partial else '')]
            text += [f' "{name}" : {value:.6f}' for name, value in values]
            text += ['```', '']
    text += ['| Activity | Minutes | Reported tokens |', '| --- | ---: | ---: |']
    text += [f'| {name} | {v[0]/60:.1f} | {v[1]} |' for name, v in totals.items()]
    text += ['', '</details>', '', '<details><summary>Execution graph</summary>', '',
             *graph(data, compact=False, limit=35 if bounded else None), '', '</details>', '',
             '<details><summary>Stage outcomes and accounting</summary>', '']
    for row in rows:
        heading = f"**{row.get('sequence', '—')}: {safe(row['role'], 80)} · {safe(row['status'], 80)}**"
        choice = ' / '.join(safe(row[k], 80) for k in ('provider', 'model', 'effort'))
        duration = f"{row['seconds']/60:.2f} minutes" if row.get('duration_known', True) else 'duration unknown'
        lines = [heading, '', f"{choice}; {duration}; tokens: {row['tokens'] if row['tokens'] is not None else 'unknown'}.", '']
        if row.get('reused_from'):
            lines += [f"Reuses stage {data['stage_numbers'].get(row['reused_from'], 'unknown')}; no new model usage.", '']
        if row.get('summary'):
            lines += [safe(row['summary']), '']
        if bounded and len('\n'.join(text + lines)) > 29000:
            text += ['Further details omitted; the local report retains all records.', '']
            break
        text += lines
    text += ['</details>', '']
    return '\n'.join(text)


def generate(directory):
    root = Path(directory).resolve()
    state = read_json(root / 'session.json', {})
    if state.get('kind') != 'issue' or state.get('execution_trace_version') != 2:
        raise ValueError('Reports support only new issue sessions created with version-2 tracing. Existing workflows can still resume normally.')
    trace = session_trace.validate(read_json(root / 'execution-trace.json'), state['id'])
    ledger = read_json(root / 'usage/ledger.json', {})
    calls = ledger.get('calls', {})
    data = report_data(trace, calls, outcome=state.get('phase', 'unknown'))
    return write_report(root / 'reports', data)


def report_data(trace, calls, *, outcome='unknown', shared_ledger=False):
    """Build a report from explicit trace identities, optionally in a shared ledger.

    Owner batches share their session ledger. Unrelated ledger calls must not
    affect the batch's accounting or coverage; missing referenced calls still do.
    """
    session_trace.validate(trace)
    rows, claimed = [], set()
    coverage = trace.get('coverage', 'partial')
    for stage in trace['stages']:
        call_ids = [key for key in stage.get('call_ids', []) if key not in claimed]
        claimed.update(call_ids)
        observations = [calls[key] for key in call_ids if key in calls]
        known = [c['usage']['total_tokens'] for c in observations if c.get('usage') is not None]
        if len(observations) != len(call_ids) or stage['status'] in ('running', 'interrupted') or stage.get('timing_coverage') == 'unknown':
            coverage = 'partial'
        choice = stage.get('choice') or {}
        seconds = stage.get('seconds', 0)
        if stage['status'] == 'reused' or stage['role'].startswith('control/'):
            seconds = 0
        rows.append(dict(id=stage['id'], sequence=stage['sequence'], stage=True, role=stage['role'],
            category=category(stage['role']), status=('reused: ' + stage['outcome']) if stage['status'] == 'reused' else stage['outcome'], context=stage.get('context', {}),
            provider=str(choice.get('provider', 'controller')), model=str(choice.get('model', 'none')), effort=str(choice.get('effort', 'none')),
            seconds=seconds, duration_known='seconds' in stage and stage.get('timing_coverage') != 'unknown', tokens=sum(known) if known else None, invocation=stage['invocation'],
            summary=stage.get('summary', ''), reused_from=stage.get('reused_from')))
    # Unassigned calls (e.g. authentication probes) are accounting observations,
    # never inferred workflow stages or dependency edges.
    for execution in trace['executions']:
        key = execution.get('call_id')
        if not key or key in claimed:
            continue
        claimed.add(key)
        call = calls.get(key, {})
        rows.append(dict(id=execution['id'], role=execution['role'], category='Other', stage=False,
            status=execution['status'], seconds=execution.get('seconds', 0), invocation=execution['invocation'],
            provider=call.get('provider', 'unknown'), model=call.get('model', 'unknown'), effort=call.get('effort', 'unknown'),
            tokens=(call.get('usage') or {}).get('total_tokens')))
    if not shared_ledger and set(calls) - claimed:
        coverage = 'partial'  # No inference from unmatched usage to missing stages.
    for invocation in trace['invocations']:
        if 'seconds' not in invocation:
            coverage = 'partial'
            continue
        measured = sum(r['seconds'] for r in rows if r['invocation'] == invocation['id'])
        overhead = max(0, invocation['seconds'] - measured)
        if overhead:
            rows.append(dict(id='overhead-' + invocation['id'], role='Orchestration overhead', category='Other', stage=False,
                status=invocation['status'], seconds=overhead, invocation=invocation['id'], tokens=None,
                provider='controller', model='none', effort='none'))
    observed_calls = [calls[key] for key in claimed if key in calls]
    return dict(version=2, outcome=trace.get('outcome', outcome), activities=rows, transitions=trace['transitions'],
                coverage=coverage, waiting_seconds=trace.get('waiting_seconds'),
                reported_calls=sum(c.get('usage') is not None for c in observed_calls), total_calls=len(claimed),
                token_partial=any(c.get('coverage') != 'reported' for c in observed_calls),
                repairs=sum(s['role'] == 'control/repair_allocated' for s in trace['stages']),
                stage_numbers={s['id']: s['sequence'] for s in trace['stages']})


def write_report(output, data):
    output = Path(output)
    output.mkdir(exist_ok=True, mode=0o700)
    atomic_json(output / 'session.json', data)
    path = output / 'session.md'
    atomic_bytes(path, render(data).encode())
    return {'report_path': str(path), 'report_data_path': str(output / 'session.json')}, render(data, bounded=True)


def finish(flow, result):
    if not flow.state.get('session_report'):
        return result
    try:
        fields, body = generate(flow.path)
        result.update(fields)
        if flow.state.get('phase') in ('ready', 'complete') and result.get('exit_code', 0) == 0:
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
