#!/usr/bin/env python3
"""Paired evaluation of current ("control") versus strengthened issue planning.

Pure functions (manifest validation, run matrix, usage accounting, summaries) are
separate from the live orchestration so that ``tests/test_evaluation_runner.py``
can exercise them without a model. Live runs consume provider quota and are never
started by the deterministic suite.

    python3 evaluate.py prepare                      # no model calls
    python3 evaluate.py verify-checks                # checks fail on baseline, pass on reference
    python3 evaluate.py run --out DIR [--tasks a,b]  # live; resumable
    python3 evaluate.py rescore results/round1 ...   # re-run the checks on retained candidate diffs
    python3 evaluate.py summarize --out DIR          # results.json
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'lib'))

UNAVAILABLE = 'unavailable'
USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(path):
    """Digest of relative names and bytes of every file below ``path`` (no caches)."""
    digest = hashlib.sha256()
    for file in sorted(p for p in Path(path).rglob('*') if p.is_file() and '__pycache__' not in p.parts):
        digest.update(file.relative_to(path).as_posix().encode() + b'\0' + file.read_bytes() + b'\0')
    return digest.hexdigest()


# ---- manifest and matrix -------------------------------------------------------------------------------
def load_manifest(path=None):
    return json.loads(Path(path or HERE / 'manifest.json').read_text())


def load_task(name):
    return json.loads((HERE / 'tasks' / name / 'task.json').read_text())


def validate_manifest(manifest, tasks=None):
    """Return a list of problems; an empty list means the design meets the approved minimum."""
    problems = []
    tasks = tasks or {name: load_task(name) for name in manifest['tasks']}
    sizes = [t['size'] for t in tasks.values()]
    if sizes.count('small') < 2 or sizes.count('complex') < 2:
        problems.append('need at least two small and two complex tasks')
    if not any(t.get('answers') for t in tasks.values()):
        problems.append('need a clarification case with predetermined answers')
    if set(manifest['implementation_configs']) != {'economical', 'default'}:
        problems.append('need exactly the economical and default implementation configurations')
    for name, choice in {**manifest['implementation_configs'], 'planning': manifest['planning'],
                         'review': manifest['review']}.items():
        if set(choice) < {'provider', 'model', 'effort'} or not all(choice[k] for k in ('provider', 'model', 'effort')):
            problems.append(f'{name} must freeze provider, model and effort')
    if set(manifest['treatments']) != {'control', 'strengthened'}:
        problems.append('treatments must be control and strengthened')
    runs = matrix(manifest, tasks)
    if len(runs) < 16:
        problems.append(f'only {len(runs)} implementation runs; at least 16 are required')
    by_pair = {}
    for run in runs:
        by_pair.setdefault(run['pair'], []).append(run)
    for pair, members in by_pair.items():
        if sorted(r['arm'] for r in members) != ['control', 'strengthened']:
            problems.append(f'pair {pair} lacks one run per arm')
        if len({json.dumps(settings_for(manifest, r), sort_keys=True) for r in members}) != 1:
            problems.append(f'pair {pair} differs in more than the planning treatment')
        if sorted(r['position'] for r in members) != [0, 1]:
            problems.append(f'pair {pair} has no defined order')
    firsts = [next(r['arm'] for r in members if r['position'] == 0) for _, members in sorted(by_pair.items())]
    if len(set(firsts)) < 2:
        problems.append('execution order does not alternate')
    return problems


def matrix(manifest, tasks=None):
    """One run per (task, implementation configuration, arm); order alternates by pair index."""
    runs, index = [], 0
    for task in manifest['tasks']:
        for config in manifest['implementation_configs']:
            arms = ['control', 'strengthened'] if index % 2 == 0 else ['strengthened', 'control']
            pair = f'{task}/{config}'
            for position, arm in enumerate(arms):
                runs.append({'id': f'{task}.{config}.{arm}', 'pair': pair, 'task': task, 'config': config,
                             'arm': arm, 'position': position, 'pair_index': index})
            index += 1
    return runs


def settings_for(manifest, run):
    """Everything that must be identical within a pair: only the planning treatment may differ."""
    return {'planning': manifest['planning'], 'implementation': manifest['implementation_configs'][run['config']],
            'review': manifest['review'], 'limits': manifest['limits'], 'fixture': manifest['fixture']['tree_sha256'],
            'task': run['task'], 'prompts': manifest['prompts']}


# ---- usage accounting ----------------------------------------------------------------------------------
def usage_from_events(events):
    """Provider-reported usage summed over completed turns; never guessed.

    A call with no usage record is ``unavailable``, which is different from zero.
    """
    total, seen = dict.fromkeys(USAGE_FIELDS, 0), 0
    cost, cost_seen = 0.0, False
    for event in events:
        usage = event.get('usage') if isinstance(event, dict) and event.get('type') in ('turn.completed', 'result') else None
        if isinstance(usage, dict):
            seen += 1
            total['input_tokens'] += _count(usage.get('input_tokens')) + _count(usage.get('cache_read_input_tokens')) \
                + _count(usage.get('cache_creation_input_tokens'))
            total['cached_input_tokens'] += _count(usage.get('cached_input_tokens')) + _count(usage.get('cache_read_input_tokens'))
            total['output_tokens'] += _count(usage.get('output_tokens'))
            total['reasoning_output_tokens'] += _count(usage.get('reasoning_output_tokens'))
        if isinstance(event, dict) and event.get('type') == 'result' and isinstance(event.get('total_cost_usd'), (int, float)):
            cost, cost_seen = cost + event['total_cost_usd'], True
    if not seen:
        return {'status': UNAVAILABLE}
    return {'status': 'reported', 'turns_reported': seen, **total, 'provider_cost_usd': cost if cost_seen else UNAVAILABLE}


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def merge_usage(usages):
    """Sum several usages. Any unavailable member makes the total partial, never silently complete."""
    usages = list(usages)
    reported = [u for u in usages if u.get('status') == 'reported']
    missing = len(usages) - len(reported)
    if not reported:
        return {'status': UNAVAILABLE, 'calls': len(usages), 'calls_unavailable': missing}
    total = {k: sum(u[k] for u in reported) for k in USAGE_FIELDS}
    costs = [u.get('provider_cost_usd') for u in reported]
    return {'status': 'partial' if missing else 'reported', 'calls': len(usages), 'calls_unavailable': missing, **total,
            'provider_cost_usd': sum(costs) if all(isinstance(c, (int, float)) for c in costs) else UNAVAILABLE}


def add_seconds(values):
    values = list(values)
    return round(sum(v for v in values if isinstance(v, (int, float))), 3) if values else 0.0


# ---- scoring and summary -------------------------------------------------------------------------------
def score(task, check_output):
    """Acceptance coverage and correctness from the independent checks. Missing results are failures."""
    acceptance = (check_output or {}).get('acceptance', {})
    regression = (check_output or {}).get('regression', {})
    passed = [name for name in task['acceptance'] if acceptance.get(name) is True]
    kept = [name for name in task['regression'] if regression.get(name) is True]
    return {'acceptance_passed': len(passed), 'acceptance_total': len(task['acceptance']),
            'acceptance_coverage': round(len(passed) / len(task['acceptance']), 4),
            'regression_passed': len(kept), 'regression_total': len(task['regression']),
            'failed_acceptance': [n for n in task['acceptance'] if n not in passed],
            'failed_regression': [n for n in task['regression'] if n not in kept],
            'checks_ran': check_output is not None,
            'correct': len(passed) == len(task['acceptance']) and len(kept) == len(task['regression'])}


def end_to_end(planning, implementation):
    """Planning, implementation and review stay separate; the end-to-end view adds them explicitly."""
    unavailable = {'status': UNAVAILABLE}
    return {'elapsed_seconds': add_seconds([planning.get('elapsed_seconds'), implementation.get('elapsed_seconds')]),
            'usage': merge_usage([planning.get('usage', unavailable), implementation.get('usage', unavailable),
                                  implementation.get('review_usage', unavailable)])}


def summarize(manifest, records, tasks=None):
    """Aggregate records without dropping failures, interruptions or missing metrics."""
    tasks = tasks or {name: load_task(name) for name in manifest['tasks']}
    expected = {r['id']: r for r in matrix(manifest, tasks)}
    by_id = {r['id']: r for r in records}
    arms = {arm: {'runs': 0, 'completed': 0, 'correct': 0, 'failed': 0, 'missing': 0, 'acceptance_passed': 0,
                  'acceptance_total': 0, 'repair_rounds': 0, 'implementation_seconds': 0.0, 'planning_seconds': 0.0,
                  'clarification_turns': 0, 'usage_unavailable_runs': 0}
            for arm in manifest['treatments']}
    pairs = {}
    for run_id, run in expected.items():
        total = arms[run['arm']]
        total['runs'] += 1
        record = by_id.get(run_id)
        if record is None:
            total['missing'] += 1
            pairs.setdefault(run['pair'], {})[run['arm']] = {'status': 'not_run'}
            continue
        failed = record['status'] != 'completed'
        total['failed'] += failed
        total['completed'] += not failed
        total['correct'] += bool(record['score']['correct'])
        total['acceptance_passed'] += record['score']['acceptance_passed']
        total['acceptance_total'] += record['score']['acceptance_total']
        total['repair_rounds'] += record['implementation'].get('repair_rounds', 0)
        total['implementation_seconds'] = add_seconds([total['implementation_seconds'], record['implementation'].get('elapsed_seconds')])
        total['planning_seconds'] = add_seconds([total['planning_seconds'], record['planning'].get('elapsed_seconds')])
        total['clarification_turns'] += record['planning'].get('clarification_turns', 0) \
            + record['implementation'].get('clarification_turns', 0)
        total['usage_unavailable_runs'] += record['end_to_end']['usage']['status'] != 'reported'
        pairs.setdefault(run['pair'], {})[run['arm']] = {
            'status': record['status'], 'correct': record['score']['correct'],
            'acceptance': f"{record['score']['acceptance_passed']}/{record['score']['acceptance_total']}",
            'repair_rounds': record['implementation'].get('repair_rounds', 0),
            'planning_seconds': record['planning'].get('elapsed_seconds'),
            'implementation_seconds': record['implementation'].get('elapsed_seconds'),
            'end_to_end_seconds': record['end_to_end']['elapsed_seconds'],
            'usage_status': record['end_to_end']['usage']['status']}
    return {'arms': arms, 'pairs': pairs, 'expected_runs': len(expected), 'recorded_runs': len(by_id),
            'not_run': sorted(set(expected) - set(by_id))}


def forbidden_claims(text):
    """Guard for generated prose: token counts must never be presented as a quota percentage."""
    return [sentence for sentence in re.split(r'(?<=[.!?])\s+|\n', text)
            if re.search(r'quota|subscription', sentence, re.I) and re.search(r'%|\bpercent', sentence, re.I)]


# ---- loading the control and strengthened planners ----------------------------------------------------
def planner_module(manifest, treatment):
    if treatment == 'strengthened':
        from pr_review import issue_planning
        return issue_planning
    control = manifest['control']
    path = HERE / control['file']
    if sha256(path.read_bytes()) != control['sha256']:
        raise SystemExit('The frozen control planner was modified; refusing to run.')
    name = 'pr_review.issue_planning_control'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def treatment_hashes():
    files = ['lib/pr_review/issue_planning.py', 'lib/pr_review/specification.py', 'prompts/issue-specification.txt']
    return {f: sha256((ROOT / f).read_bytes()) for f in files}


# ---- planning ------------------------------------------------------------------------------------------
def issue_facts(task):
    return {'number': task['number'], 'title': task['title'], 'body': task['body'], 'state': 'open', 'assignees': [],
            'html_url': f"https://github.com/example/ledger/issues/{task['number']}"}


def read_events(directory):
    events = []
    for path in sorted(Path(directory).glob('*/events.jsonl')) + sorted(Path(directory).glob('events.jsonl')):
        for line in path.read_text(errors='replace').splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def plan_task(manifest, task, treatment, directory, checkout, assess=None, clock=time.monotonic):
    """Plan with ``treatment``; supply predetermined answers only when it asks and only once."""
    from pr_review import specification
    from pr_review.common import ApiError
    assess = assess or planner_module(manifest, treatment).assess_issue
    configuration = {'processing': {k: manifest['planning'][k] for k in ('provider', 'model', 'effort')}}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    comments, turns, answered = [], [], False
    record = {'treatment': treatment, 'task': task['id'], 'status': 'failed', 'turns': turns, 'clarification_turns': 0,
              'document': None, 'questions': []}
    started = clock()
    max_turns = manifest['planning'].get('max_clarification_turns', 2) + 1
    for number in range(1, max_turns + 1):
        context = {'issue': issue_facts(task), 'comments': copy.deepcopy(comments), 'previous_plan': None,
                   'execution_blockers': [], 'pr_feedback': []}
        turn_dir = directory / f'turn-{number}'
        turn_started = clock()
        turn = {'turn': number, 'context': context}
        turns.append(turn)
        try:
            value = assess(context, configuration, turn_dir, checkout)
        except (ApiError, OSError, ValueError) as exc:
            turn.update(status='failed', error=f'{getattr(exc, "code", type(exc).__name__)}: {exc}')
            turn['elapsed_seconds'] = round(clock() - turn_started, 3)
            turn['usage'] = usage_from_events(read_events(turn_dir))
            record['status'] = 'planning_failed'
            break
        except BaseException as exc:                     # interruption: keep what exists, mark it, stop the run command
            turn.update(status='interrupted', error=f'{type(exc).__name__}: {exc}')
            turn['elapsed_seconds'] = round(clock() - turn_started, 3)
            turn['usage'] = usage_from_events(read_events(turn_dir))
            record['status'] = 'planning_interrupted'
            break
        turn['elapsed_seconds'] = round(clock() - turn_started, 3)
        turn['usage'] = usage_from_events(read_events(turn_dir))
        turn.update(status='ok', decision=value['decision'], response={k: v for k, v in value.items() if k != 'agent_configuration'})
        if value['decision'] == 'questions':
            record['questions'].append(value['questions'])
            if task.get('answers') and not answered:
                answered = True
                record['clarification_turns'] += 1
                comments.append({'id': 9000 + number, 'user': {'login': 'maintainer'}, 'body': task['answers']})
                continue
            record['status'] = 'unanswered_questions'
            break
        if value['decision'] == 'plan':
            document = specification.render(value['plan'], value['acceptance'])
            record.update(status='planned', document=document, document_sha256=sha256(document.encode('utf-8')))
            break
        record['status'] = 'no_plan_' + value['decision']
        break
    record['planner_hashes'] = treatment_hashes() if treatment == 'strengthened' else {manifest['control']['file']: manifest['control']['sha256']}
    record['answers_supplied'] = answered
    record['elapsed_seconds'] = round(clock() - started, 3)
    record['usage'] = merge_usage(t['usage'] for t in turns) if turns else {'status': UNAVAILABLE}
    record['comments'] = comments
    return record


# ---- implementation ------------------------------------------------------------------------------------
REVIEW_PROMPT = '''Independently review a candidate implementation against an approved specification.
Read the complete Markdown specification file named by approved_specification.file, including its
acceptance criteria. The candidate is the working tree of the repository in your current directory; the
baseline commit is named in the context and `git diff <baseline>` shows the change. Run the repository tests and
exercise the behavior the specification describes. You may not modify any file. Judge the observable
outcome, not the route taken. Report changes_requested only for a concrete violation of the specification or a
regression, listing each as a self-contained finding; optional improvements are never blocking. Otherwise
report passed.
'''


def review_schema():
    from pr_review.owner_providers import obj, TEXT, STRINGS
    return obj({'assessment': {'type': 'string', 'enum': ['passed', 'changes_requested']}, 'summary': TEXT, 'findings': STRINGS})


def baseline_checkout(manifest, destination):
    """A fresh workspace; a leftover one (from an interrupted attempt) is discarded and rebuilt."""
    if Path(destination).exists():
        shutil.rmtree(destination)
    shutil.copytree(HERE / manifest['fixture']['path'], destination, ignore=shutil.ignore_patterns('__pycache__'))
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'Eval', 'GIT_AUTHOR_EMAIL': 'eval@example.com',
           'GIT_COMMITTER_NAME': 'Eval', 'GIT_COMMITTER_EMAIL': 'eval@example.com', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    for args in (['init', '-q', '-b', 'main'], ['add', '-A'], ['commit', '-q', '-m', 'baseline']):
        subprocess.run(['git', *args], cwd=destination, check=True, env=env, capture_output=True)
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=destination, check=True, text=True, capture_output=True).stdout.strip()


def run_checks(manifest, task, checkout, timeout=300):
    """Independent checks execute outside the model workspace and print one JSON line."""
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
           'LEDGER_ORIGINAL_TESTS': str(HERE / manifest['fixture']['path'] / 'tests')}
    with tempfile.TemporaryDirectory(prefix='ledger-check-run-') as scratch:
        try:
            done = subprocess.run([sys.executable, str(HERE / 'checks' / f"{task['id']}.py"), str(checkout)], cwd=scratch,
                                  env=env, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    lines = [line for line in done.stdout.splitlines() if line.startswith('{')]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        return None


def event_scan(directory, needles):
    """Count references in model event logs to files that must stay hidden from the workspace."""
    hits = 0
    for path in Path(directory).rglob('events.jsonl'):
        text = path.read_text(errors='replace')
        hits += sum(text.count(n) for n in needles)
    return hits


def sanitize(value, replacements):
    text = json.dumps(value, ensure_ascii=False)
    for old, new in replacements:
        text = text.replace(old, new)
    return json.loads(text)


def provider_call(choice, prompt, directory, cwd, schema, conversation, timeout, runner=None, role='fix'):
    """One provider turn through the production argv builders; returns a record, never raises on provider failure."""
    from pr_review import issue_harness, owner_providers
    from pr_review.common import ApiError
    from pr_review.processes import run
    from pr_review.providers import Runtime, environment
    from pr_review.structured import response
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'scratch').mkdir(exist_ok=True)
    schema_path = directory / 'schema.json'
    schema_path.write_text(json.dumps(schema, indent=2))
    (directory / 'prompt.txt').write_text(prompt)
    if role == 'fix':
        args = issue_harness.arguments(choice, directory, schema_path, conversation)
    else:
        args = owner_providers.argv(choice, Runtime.environment(), 'review', directory, schema_path, cwd=cwd)
    env, _ = environment(choice['provider'])
    env.update(TMPDIR=str(directory / 'scratch'), PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0')
    identity = {}

    def on_line(stream, line):
        try:
            event = json.loads(line)
        except ValueError:
            return
        if isinstance(event, dict) and stream == 'stdout' and event.get('type') == 'thread.started':
            identity['id'] = event.get('thread_id')
        elif isinstance(event, dict) and event.get('type') == 'system' and event.get('subtype') == 'init':
            identity['id'] = event.get('session_id')
    started = time.monotonic()
    call = {'argv': args, 'directory': str(directory)}
    try:
        result = (runner or run)(args, cwd=cwd, env=env, input_bytes=prompt.encode(), timeout=timeout,
                                 stdout_path=directory / 'events.jsonl', stderr_path=directory / 'stderr.log',
                                 model_events=True, on_line=on_line, stage=f'evaluation-{role}')
        call['elapsed_seconds'] = round(time.monotonic() - started, 3)
        call['usage'] = usage_from_events(result.events)
        try:
            call['value'] = response(choice, result, directory)
            call['status'] = 'ok'
        except ApiError as exc:
            call.update(status='failed', error=f'{exc.code}: {exc}')
    except BaseException as exc:                       # interruption or timeout: retain it, do not hide it
        call['elapsed_seconds'] = round(time.monotonic() - started, 3)
        call['usage'] = usage_from_events(read_events(directory))
        call.update(status='interrupted', error=f'{getattr(exc, "code", type(exc).__name__)}: {exc}',
                    kind='implementation' if role == 'fix' else 'review', session_id=identity.get('id'))
        if not isinstance(exc, Exception):              # Ctrl-C and friends: let the caller record the run first
            exc.call_record = call
            raise
    call['session_id'] = identity.get('id')
    return call


def implementation_context(manifest, task, planning, handle, checkout, baseline, directory, feedback):
    issue_context = {'issue_body': task['body'],
                     'answers': [{'id': c['id'], 'user': c['user'], 'body': c['body']} for c in planning.get('comments', [])]}
    return {'approved_specification': handle, 'feedback': feedback, 'worktree': str(checkout),
            'selected_feedback': [{'key': 'issue', 'kind': 'issue_plan', 'title': task['title'],
                                   'source': {'plan': {'revision': 1, 'digest': handle['sha256']}}}],
            'issue_context': issue_context, 'scratch': str(Path(directory) / 'scratch'), 'baseline': baseline,
            'prior_checks': [], 'previous_attempts': []}


def implement(manifest, task, planning, config, directory, provider=provider_call, clock=time.monotonic):
    """The shared harness loop: implement, review, repair; identical for both arms."""
    from pr_review import issue_harness, owner_providers, specification
    from pr_review.common import ApiError
    directory = Path(directory)
    limits = manifest['limits']
    choice = dict(manifest['implementation_configs'][config])
    review_choice = dict(manifest['review'])
    checkout = directory / 'repo'
    baseline = baseline_checkout(manifest, checkout)
    document = planning['document']
    spec_path = directory / 'spec' / 'revision-1.md'
    stored = specification.write(spec_path, document)
    handle = {'format': 'markdown', 'file': str(spec_path), 'revision': 1, 'sha256': stored['sha256'], 'bytes': stored['bytes']}
    prompt_text = (ROOT / 'prompts' / 'issue-implement.txt').read_text()
    conversation, calls, feedback = {'session_id': str(uuid.uuid4()) if choice['provider'] == 'claude' else None}, [], []
    answers_used = False
    record = {'status': 'failed', 'config': choice, 'turns': 0, 'repair_rounds': 0, 'format_corrections': 0,
              'continuations': 0, 'clarification_turns': 0, 'review_rounds': 0, 'calls': calls, 'stop': ''}
    started = clock()
    try:
        while True:
            if clock() - started > limits['max_run_seconds']:
                record['stop'] = 'max_run_seconds'
                break
            if record['turns'] >= limits['max_implementation_turns']:
                record['stop'] = 'max_implementation_turns'
                break
            record['turns'] += 1
            turn_dir = directory / f"turn-{record['turns']}"
            turn_dir.mkdir(parents=True, exist_ok=True)
            prompt, _ = owner_providers.context_prompt(
                prompt_text + '\n', implementation_context(manifest, task, planning, handle, checkout, baseline, turn_dir, feedback), turn_dir)
            call = provider(choice, prompt, turn_dir, checkout, copy.deepcopy(issue_harness.SCHEMA), conversation, limits['call_timeout_seconds'])
            call['kind'] = 'implementation'
            calls.append(call)
            if call.get('session_id'):
                conversation.update(session_id=call['session_id'], started=True)
            if call['status'] != 'ok':
                record['stop'] = 'provider_' + call['status']
                break
            try:
                report = issue_harness.validate(call['value'])
            except ApiError as exc:
                if record['format_corrections'] < 1:
                    record['format_corrections'] += 1
                    feedback = feedback + ['Correct the completion report to the supplied schema: ' + str(exc)]
                    continue
                record['stop'] = 'invalid_report'
                break
            call['report_status'] = report['status']
            if report['status'] == 'needs_input':
                if task.get('answers') and not answers_used:
                    answers_used = True
                    record['clarification_turns'] += 1
                    feedback = [task['answers'] + ' (Predetermined maintainer answers to your product question.)']
                    continue
                record['stop'] = 'needs_input'
                record['questions'] = report['questions']
                break
            if report['status'] in ('changes_requested', 'blocked_external'):
                record['stop'] = report['status']
                break
            if report['status'] == 'working':
                record['continuations'] += 1
                feedback = ['Continue the approved implementation to a complete candidate; own testing and debugging.']
                continue
            record['review_rounds'] += 1
            review_dir = directory / f"review-{record['review_rounds']}"
            copy_dir = directory / f"review-copy-{record['review_rounds']}"
            shutil.copytree(checkout, copy_dir)
            review_context = {'approved_specification': handle, 'baseline': baseline, 'implementer_report': report,
                              'issue_context': {'issue_body': task['body']}}
            review_prompt = REVIEW_PROMPT + '\n' + json.dumps(review_context)
            reviewed = provider(review_choice, review_prompt, review_dir, copy_dir, review_schema(), {}, limits['call_timeout_seconds'], role='review')
            reviewed['kind'] = 'review'
            calls.append(reviewed)
            shutil.rmtree(copy_dir, ignore_errors=True)
            if reviewed['status'] != 'ok':
                record['stop'] = 'review_' + reviewed['status']
                break
            verdict = reviewed['value']
            reviewed['report_status'] = verdict.get('assessment')
            if verdict.get('assessment') == 'passed':
                record['status'] = 'completed'
                record['stop'] = 'passed_review'
                break
            if record['repair_rounds'] >= limits['max_repair_rounds']:
                record['stop'] = 'max_repair_rounds'
                break
            record['repair_rounds'] += 1
            feedback = [f for f in verdict.get('findings') or [verdict.get('summary', 'Review requested changes.')]]
    except BaseException as exc:    # an interrupted run is a retained, scored result, never a lost one
        record.update(status='interrupted', stop='interrupted: ' + type(exc).__name__, interruption=str(exc))
        partial = getattr(exc, 'call_record', None)
        if partial is not None and partial not in calls:
            calls.append(partial)
    record['elapsed_seconds'] = round(clock() - started, 3)
    impl_calls = [c for c in calls if c['kind'] == 'implementation']
    review_calls = [c for c in calls if c['kind'] == 'review']
    record['usage'] = merge_usage(c['usage'] for c in impl_calls) if impl_calls else {'status': UNAVAILABLE, 'calls': 0}
    record['review_usage'] = merge_usage(c['usage'] for c in review_calls) if review_calls else {'status': UNAVAILABLE, 'calls': 0}
    record['implementation_elapsed_seconds'] = add_seconds(c.get('elapsed_seconds') for c in impl_calls)
    record['review_elapsed_seconds'] = add_seconds(c.get('elapsed_seconds') for c in review_calls)
    record['hidden_reference_hits'] = event_scan(directory, [str(HERE / 'checks'), str(HERE / 'reference'), 'LEDGER_ORIGINAL_TESTS'])
    diff = subprocess.run(['git', 'diff', '--numstat', baseline], cwd=checkout, text=True, capture_output=True).stdout
    untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard'], cwd=checkout, text=True, capture_output=True).stdout
    record['files_changed'] = sorted([line.split('\t')[2] for line in diff.splitlines() if line.count('\t') == 2] +
                                     [f for f in untracked.splitlines() if '__pycache__' not in f])
    record['commits_added'] = int(subprocess.run(['git', 'rev-list', '--count', f'{baseline}..HEAD'], cwd=checkout,
                                                 text=True, capture_output=True).stdout.strip() or 0)
    record['checkout'] = str(checkout)
    return record


def execute_run(manifest, run, task, planning, directory, provider=provider_call, checks=run_checks):
    directory = Path(directory)
    if planning['status'] != 'planned':
        implementation = {'status': 'not_started', 'stop': planning['status'], 'turns': 0, 'repair_rounds': 0,
                          'clarification_turns': 0, 'elapsed_seconds': 0.0, 'usage': {'status': UNAVAILABLE, 'calls': 0}}
        output = None
    else:
        implementation = implement(manifest, task, planning, run['config'], directory, provider)
        output = checks(manifest, task, implementation['checkout'])
    result = score(task, output)
    status = implementation['status'] if implementation['status'] in ('completed', 'interrupted') else 'failed'
    planning_view = {k: v for k, v in planning.items() if k not in ('turns', 'comments', 'document')}
    return {'id': run['id'], 'pair': run['pair'], 'task': run['task'], 'config': run['config'], 'arm': run['arm'],
            'position': run['position'], 'status': status, 'planning': planning_view, 'implementation': implementation,
            'checks': output, 'score': result, 'settings': settings_for(manifest, run),
            'end_to_end': end_to_end(planning, implementation)}


# ---- command line --------------------------------------------------------------------------------------
def command_prepare(args):
    manifest = load_manifest()
    problems = validate_manifest(manifest)
    actual = tree_digest(HERE / manifest['fixture']['path'])
    if actual != manifest['fixture']['tree_sha256']:
        problems.append(f'fixture changed: {actual}')
    if sha256((HERE / manifest['control']['file']).read_bytes()) != manifest['control']['sha256']:
        problems.append('frozen control planner changed')
    strengthened = manifest.get('strengthened', {})
    for name, digest in strengthened.get('current', {}).items():
        if sha256((ROOT / name).read_bytes()) != digest:
            problems.append(f'{name} differs from the planner recorded in the manifest (current)')
    for name, archived in strengthened.get('round1_archive', {}).items():
        if sha256((HERE / archived['file']).read_bytes()) != archived['sha256']:
            problems.append(f'archived round-1 {name} was modified')
    print(json.dumps({'runs': len(matrix(manifest)), 'problems': problems}, indent=2))
    return 1 if problems else 0


def command_verify_checks(args):
    manifest = load_manifest()
    failures = []
    for name in manifest['tasks']:
        task = load_task(name)
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch) / 'baseline'
            baseline_checkout(manifest, base)
            before = score(task, run_checks(manifest, task, base))
            reference = Path(scratch) / 'reference'
            baseline_checkout(manifest, reference)
            shutil.copytree(HERE / 'reference' / name, reference, dirs_exist_ok=True)
            after = score(task, run_checks(manifest, task, reference))
        print(json.dumps({'task': name, 'baseline': before['acceptance_passed'], 'reference': after['acceptance_passed'],
                          'total': before['acceptance_total'], 'reference_correct': after['correct']}))
        if before['correct'] or before['acceptance_passed'] == before['acceptance_total'] or not after['correct']:
            failures.append(name)
    return 1 if failures else 0


def retire_interrupted(path):
    """Keep an interrupted attempt's record beside the new one (``name.interrupted-N.json``)."""
    number = 1
    while path.with_name(f'{path.stem}.interrupted-{number}{path.suffix}').exists():
        number += 1
    path.rename(path.with_name(f'{path.stem}.interrupted-{number}{path.suffix}'))


def load_planning(target):
    """A saved plan, with the handed document read back from the exact bytes written for the model."""
    record = json.loads((target / 'planning.json').read_text())
    document = target / 'specification.md'
    if document.exists():
        data = document.read_bytes()
        if record.get('document_sha256') and sha256(data) != record['document_sha256']:
            raise SystemExit(f'{document} no longer matches its recorded digest; refusing to resume.')
        record['document'] = data.decode('utf-8')
    record.setdefault('document', None)
    return record


def run_matrix(manifest, out, tasks=None, arms=None, plan=None, execute=None, checkout=None):
    """Plan once per (task, arm), then run every pair member. Resumable; interruptions are recorded.

    Returns 0, or 130 after an interruption has been written to disk.
    """
    plan, execute = plan or plan_task, execute or execute_run
    out = Path(out).resolve()
    wanted = set(tasks) if tasks else set(manifest['tasks'])
    arms = set(arms) if arms else set(manifest['treatments'])
    replacements = [(str(out), '<OUT>'), (str(HERE), '<EVAL>'), (str(ROOT), '<ROOT>'), (str(Path.home()), '<HOME>')]
    runs = [r for r in matrix(manifest) if r['task'] in wanted and r['arm'] in arms]
    planned = {}
    for task_name in dict.fromkeys(r['task'] for r in runs):
        task = load_task(task_name)
        index = manifest['tasks'].index(task_name)
        for arm in (['control', 'strengthened'] if index % 2 == 0 else ['strengthened', 'control']):
            if arm not in arms:
                continue
            target = out / 'planning' / f'{task_name}.{arm}'
            saved = target / 'planning.json'
            if saved.exists():
                if json.loads(saved.read_text()).get('status') != 'planning_interrupted':
                    planned[(task_name, arm)] = load_planning(target)
                    continue
                retire_interrupted(saved)
            checkout_path = out / 'work' / f'plan-{task_name}.{arm}' / 'repo'
            (checkout or baseline_checkout)(manifest, checkout_path)
            record = plan(manifest, task, arm, target, checkout_path)
            if record.get('document'):
                (target / 'specification.md').write_bytes(record['document'].encode('utf-8'))
            stored = {k: v for k, v in record.items() if k != 'document'}      # the document lives only in specification.md
            saved.write_text(json.dumps(sanitize(stored, replacements), indent=2, ensure_ascii=False) + '\n')
            planned[(task_name, arm)] = record
            print('planned', task_name, arm, record['status'], flush=True)
            if record['status'] == 'planning_interrupted':
                return 130
    for run in runs:
        target = out / 'runs' / run['id']
        saved = target / 'result.json'
        if saved.exists():
            if json.loads(saved.read_text()).get('status') != 'interrupted':
                continue
            retire_interrupted(saved)
        task = load_task(run['task'])
        result = execute(manifest, run, task, planned[(run['task'], run['arm'])], out / 'work' / run['id'])
        target.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(sanitize(result, replacements), indent=2, ensure_ascii=False) + '\n')
        print('ran', run['id'], result['status'], result['score']['acceptance_coverage'], flush=True)
        if result['status'] == 'interrupted':
            return 130
    return 0


def command_run(args):
    manifest = load_manifest()
    problems = validate_manifest(manifest)
    if problems:
        raise SystemExit('Invalid manifest: ' + '; '.join(problems))
    return run_matrix(manifest, args.out, args.tasks.split(',') if args.tasks else None,
                      args.arms.split(',') if args.arms else None)


def rescore_diff(manifest, task, diff_text, scratch):
    """Apply a retained candidate diff to a fresh baseline checkout and run the independent checks on it."""
    checkout = Path(scratch) / 'rescore'
    baseline_checkout(manifest, checkout)
    if diff_text.strip():
        subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=checkout, input=diff_text, text=True, check=True, capture_output=True)
    return run_checks(manifest, task, checkout)


def command_rescore(args):
    """Re-derive check results for retained candidate diffs (writes ``rescore.json`` beside each ``changes.diff``)."""
    manifest = load_manifest()
    failures = 0
    for directory in args.results:
        for diff in sorted(Path(directory).glob('runs/*/changes.diff')):
            task = load_task(diff.parent.name.split('.')[0])
            with tempfile.TemporaryDirectory(prefix='rescore-') as scratch:
                output = rescore_diff(manifest, task, diff.read_text(), scratch)
            result = score(task, output)
            (diff.parent / 'rescore.json').write_text(json.dumps({'checks': output, 'score': result}, indent=2) + '\n')
            print('rescored', diff.parent.name, 'correct' if result['correct'] else 'NOT correct',
                  'original_tests_pass=' + str((output or {}).get('regression', {}).get('original_tests_pass')), flush=True)
            failures += not result['correct']
    return 1 if failures else 0


def command_summarize(args):
    manifest = load_manifest()
    out = Path(args.out)
    records = [json.loads(p.read_text()) for p in sorted((out / 'runs').glob('*/result.json'))]
    summary = summarize(manifest, records)
    (out / 'results.json').write_text(json.dumps({'summary': summary, 'runs': records}, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare').set_defaults(function=command_prepare)
    commands.add_parser('verify-checks').set_defaults(function=command_verify_checks)
    run = commands.add_parser('run')
    run.add_argument('--out', required=True)
    run.add_argument('--tasks')
    run.add_argument('--arms', help='comma-separated subset of arms (default: both)')
    run.set_defaults(function=command_run)
    rescore = commands.add_parser('rescore')
    rescore.add_argument('results', nargs='+', help='exported results directories containing runs/*/changes.diff')
    rescore.set_defaults(function=command_rescore)
    summary = commands.add_parser('summarize')
    summary.add_argument('--out', required=True)
    summary.set_defaults(function=command_summarize)
    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == '__main__':
    sys.exit(main())
