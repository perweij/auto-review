"""Deterministic tests of the planning-evaluation runner; no model is ever launched."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
EVAL = ROOT / 'docs' / 'evaluations' / 'specification-planning'
_spec = importlib.util.spec_from_file_location('specification_planning_evaluate', EVAL / 'evaluate.py')
evaluate = importlib.util.module_from_spec(_spec)
sys.modules['specification_planning_evaluate'] = evaluate
_spec.loader.exec_module(evaluate)

from pr_review import specification
from pr_review.common import ApiError


def manifest():
    return evaluate.load_manifest()


def tasks():
    return {name: evaluate.load_task(name) for name in manifest()['tasks']}


class ManifestTest(unittest.TestCase):
    def test_the_frozen_design_meets_the_approved_minimum(self):
        self.assertEqual([], evaluate.validate_manifest(manifest()))
        by_size = [t['size'] for t in tasks().values()]
        self.assertGreaterEqual(by_size.count('small'), 2)
        self.assertGreaterEqual(by_size.count('complex'), 2)
        self.assertEqual(16, len(evaluate.matrix(manifest())))
        self.assertTrue(any(t.get('answers') for t in tasks().values()))

    def test_provider_model_and_effort_are_frozen_for_every_role(self):
        m = manifest()
        for choice in [m['planning'], m['review'], *m['implementation_configs'].values()]:
            self.assertTrue(all(choice.get(k) for k in ('provider', 'model', 'effort')), choice)
        self.assertNotEqual(m['implementation_configs']['economical'], m['implementation_configs']['default'])

    def test_every_pair_varies_only_the_planning_treatment(self):
        m = manifest()
        runs = evaluate.matrix(m)
        pairs = {}
        for run in runs:
            pairs.setdefault(run['pair'], []).append(run)
        self.assertEqual(8, len(pairs))
        for pair, members in pairs.items():
            self.assertEqual({'control', 'strengthened'}, {r['arm'] for r in members}, pair)
            settings = [evaluate.settings_for(m, r) for r in members]
            self.assertEqual(settings[0], settings[1], pair)
            self.assertEqual(1, len({r['config'] for r in members}))
            self.assertEqual(1, len({r['task'] for r in members}))

    def test_execution_order_alternates_between_pairs(self):
        runs = evaluate.matrix(manifest())
        firsts = [next(r['arm'] for r in runs if r['pair'] == pair and r['position'] == 0)
                  for pair in dict.fromkeys(r['pair'] for r in runs)]
        self.assertEqual(['control', 'strengthened'] * 4, firsts)

    def test_a_design_that_breaks_pairing_or_alternation_is_rejected(self):
        m = manifest()
        broken = copy.deepcopy(m)
        broken['tasks'] = broken['tasks'][:3]
        self.assertTrue(any('at least' in p for p in evaluate.validate_manifest(broken)))
        broken = copy.deepcopy(m)
        broken['review'].pop('effort')
        self.assertTrue(any('review must freeze' in p for p in evaluate.validate_manifest(broken)))
        broken = copy.deepcopy(m)
        broken['treatments'] = {'control': 'x'}
        self.assertTrue(any('treatments' in p for p in evaluate.validate_manifest(broken)))
        original = evaluate.matrix
        try:
            evaluate.matrix = lambda manifest_, tasks_=None: [dict(r, position=0) for r in original(manifest_)]
            self.assertTrue(any('no defined order' in p for p in evaluate.validate_manifest(m)))
            evaluate.matrix = lambda manifest_, tasks_=None: [dict(r, arm='control') for r in original(manifest_)]
            self.assertTrue(any('lacks one run per arm' in p for p in evaluate.validate_manifest(m)))
            evaluate.matrix = lambda manifest_, tasks_=None: [dict(r, position=1 - r['position'] if r['pair_index'] % 2 else r['position'])
                                                              for r in original(manifest_)]
            self.assertTrue(any('does not alternate' in p for p in evaluate.validate_manifest(m)))
        finally:
            evaluate.matrix = original

    def test_pair_settings_differing_beyond_the_arm_are_detected(self):
        m = manifest()
        original = evaluate.settings_for
        try:
            evaluate.settings_for = lambda manifest_, run: {**original(manifest_, run), 'arm_leak': run['arm']}
            self.assertTrue(any('more than the planning treatment' in p for p in evaluate.validate_manifest(m)))
        finally:
            evaluate.settings_for = original

    def test_the_control_is_the_unmodified_pre_change_planner(self):
        m = manifest()
        control = EVAL / m['control']['file']
        self.assertEqual(m['control']['sha256'], evaluate.sha256(control.read_bytes()))
        text = control.read_text()
        self.assertNotIn('specification', text.split('def assess_issue')[0].lower().replace('specification_', ''))
        self.assertIn("'plan', 'questions', 'unsuitable', 'no_change', 'continue'", text)
        self.assertNotIn('inspected', text)
        module = evaluate.planner_module(m, 'control')
        self.assertTrue(callable(module.assess_issue))
        self.assertNotIn('inspected', module.SCHEMA['properties'])

    def test_the_fixture_is_frozen(self):
        m = manifest()
        self.assertEqual(m['fixture']['tree_sha256'], evaluate.tree_digest(EVAL / m['fixture']['path']))

    def test_tampering_with_the_control_stops_the_runner(self):
        m = manifest()
        m['control']['sha256'] = '0' * 64
        with self.assertRaises(SystemExit):
            evaluate.planner_module(m, 'control')


class UsageTest(unittest.TestCase):
    def test_provider_reported_usage_is_summed_over_completed_turns(self):
        events = [{'type': 'turn.started'},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 5,
                                                         'reasoning_output_tokens': 2}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 1}}]
        usage = evaluate.usage_from_events(events)
        self.assertEqual(('reported', 110, 60, 6, 2, 2), (usage['status'], usage['input_tokens'], usage['cached_input_tokens'],
                                                           usage['output_tokens'], usage['reasoning_output_tokens'], usage['turns_reported']))
        self.assertEqual('unavailable', usage['provider_cost_usd'])

    def test_claude_result_usage_and_cost_are_recorded_without_conversion(self):
        usage = evaluate.usage_from_events([{'type': 'result', 'usage': {'input_tokens': 7, 'cache_read_input_tokens': 3,
                                                                         'output_tokens': 4}, 'total_cost_usd': 0.25}])
        self.assertEqual((10, 3, 4, 0.25), (usage['input_tokens'], usage['cached_input_tokens'], usage['output_tokens'], usage['provider_cost_usd']))

    def test_missing_usage_is_unavailable_never_zero(self):
        for events in ([], [{'type': 'turn.completed'}], [{'type': 'item.completed'}], [{'type': 'turn.completed', 'usage': 'n/a'}]):
            usage = evaluate.usage_from_events(events)
            self.assertEqual({'status': 'unavailable'}, usage)
            self.assertNotIn('input_tokens', usage)

    def test_merging_marks_partial_totals_instead_of_hiding_gaps(self):
        reported = evaluate.usage_from_events([{'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 1}}])
        merged = evaluate.merge_usage([reported, {'status': 'unavailable'}, reported])
        self.assertEqual(('partial', 10, 3, 1), (merged['status'], merged['input_tokens'], merged['calls'], merged['calls_unavailable']))
        none = evaluate.merge_usage([{'status': 'unavailable'}, {'status': 'unavailable'}])
        self.assertEqual(('unavailable', 2), (none['status'], none['calls_unavailable']))
        self.assertNotIn('input_tokens', none)
        full = evaluate.merge_usage([reported, reported])
        self.assertEqual('reported', full['status'])

    def test_no_result_field_claims_a_subscription_quota_percentage(self):
        self.assertEqual([], evaluate.forbidden_claims('Used 12,000 tokens (about 3 minutes).'))
        self.assertTrue(evaluate.forbidden_claims('That is 4% of the subscription quota'))
        self.assertTrue(evaluate.forbidden_claims('quota use was 7 %'))


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.task = tasks()['list-limit']

    def passing(self):
        return {'acceptance': {n: True for n in self.task['acceptance']}, 'regression': {n: True for n in self.task['regression']}}

    def test_all_checks_passing_is_correct(self):
        result = evaluate.score(self.task, self.passing())
        self.assertTrue(result['correct'])
        self.assertEqual(1.0, result['acceptance_coverage'])

    def test_missing_or_crashed_checks_count_as_failures(self):
        self.assertFalse(evaluate.score(self.task, None)['correct'])
        self.assertEqual(0.0, evaluate.score(self.task, None)['acceptance_coverage'])
        partial = self.passing()
        partial['acceptance'].pop(self.task['acceptance'][0])
        partial['regression'][self.task['regression'][0]] = False
        result = evaluate.score(self.task, partial)
        self.assertFalse(result['correct'])
        self.assertEqual([self.task['acceptance'][0]], result['failed_acceptance'])
        self.assertEqual([self.task['regression'][0]], result['failed_regression'])
        self.assertEqual(0.75, result['acceptance_coverage'])

    def test_a_non_boolean_pass_is_not_a_pass(self):
        value = self.passing()
        value['acceptance'][self.task['acceptance'][0]] = 'yes'
        self.assertFalse(evaluate.score(self.task, value)['correct'])


class SummaryTest(unittest.TestCase):
    def record(self, run, **overrides):
        task = evaluate.load_task(run['task'])
        output = {'acceptance': {n: True for n in task['acceptance']}, 'regression': {n: True for n in task['regression']}}
        reported = {'status': 'reported', 'calls': 1, 'calls_unavailable': 0, 'input_tokens': 10, 'cached_input_tokens': 5,
                    'output_tokens': 2, 'reasoning_output_tokens': 0, 'provider_cost_usd': 'unavailable'}
        planning = {'elapsed_seconds': 5.0, 'usage': reported, 'clarification_turns': 0, 'status': 'planned'}
        implementation = {'status': 'completed', 'elapsed_seconds': 20.0, 'repair_rounds': 1, 'clarification_turns': 0,
                          'usage': reported, 'review_usage': reported}
        value = {'id': run['id'], 'pair': run['pair'], 'status': 'completed', 'planning': planning,
                 'implementation': implementation, 'score': evaluate.score(task, output)}
        value['end_to_end'] = evaluate.end_to_end(planning, implementation)
        value.update(overrides)
        return value

    def test_failures_missing_runs_and_missing_metrics_are_retained(self):
        m = manifest()
        runs = evaluate.matrix(m)
        records = [self.record(r) for r in runs[:6]]
        failed = self.record(runs[6], status='failed', score=evaluate.score(evaluate.load_task(runs[6]['task']), None))
        failed['implementation'] = {**failed['implementation'], 'status': 'failed', 'usage': {'status': 'unavailable'},
                                    'review_usage': {'status': 'unavailable'}}
        failed['end_to_end'] = evaluate.end_to_end(failed['planning'], failed['implementation'])
        records.append(failed)
        summary = evaluate.summarize(m, records)
        self.assertEqual(16, summary['expected_runs'])
        self.assertEqual(7, summary['recorded_runs'])
        self.assertEqual(9, len(summary['not_run']))
        arms = summary['arms']
        self.assertEqual(16, arms['control']['runs'] + arms['strengthened']['runs'])
        self.assertEqual(1, arms['control']['failed'] + arms['strengthened']['failed'])
        self.assertEqual(7, sum(a['completed'] + a['failed'] for a in arms.values()))
        self.assertEqual(9, sum(a['missing'] for a in arms.values()))
        self.assertEqual(1, sum(a['usage_unavailable_runs'] for a in arms.values()))     # the failed run's gap stays visible
        failed_pair = summary['pairs'][runs[6]['pair']][runs[6]['arm']]
        self.assertEqual('failed', failed_pair['status'])
        self.assertFalse(failed_pair['correct'])
        self.assertEqual('not_run', summary['pairs'][runs[15]['pair']][runs[15]['arm']]['status'])

    def test_planning_and_implementation_are_recorded_separately_and_end_to_end(self):
        run = evaluate.matrix(manifest())[0]
        record = self.record(run)
        self.assertEqual(5.0, record['planning']['elapsed_seconds'])
        self.assertEqual(20.0, record['implementation']['elapsed_seconds'])
        self.assertEqual(25.0, record['end_to_end']['elapsed_seconds'])
        self.assertEqual(30, record['end_to_end']['usage']['input_tokens'])
        self.assertEqual(3, record['end_to_end']['usage']['calls'])

    def test_unavailable_usage_stays_visible_in_the_end_to_end_total(self):
        planning = {'elapsed_seconds': 1.0, 'usage': {'status': 'unavailable'}}
        implementation = {'elapsed_seconds': 2.0, 'usage': {'status': 'unavailable'}}
        total = evaluate.end_to_end(planning, implementation)
        self.assertEqual('unavailable', total['usage']['status'])
        self.assertEqual(3.0, total['elapsed_seconds'])

    def test_summary_contains_no_quota_percentage_fields(self):
        text = json.dumps(evaluate.summarize(manifest(), [])).lower()
        for word in ('quota', 'percent', 'subscription'):
            self.assertNotIn(word, text)


class FakeAssess:
    """Stand-in for assess_issue that records its inputs and writes provider-style events."""
    def __init__(self, responses, usage=True):
        self.responses, self.usage, self.calls = list(responses), usage, []

    def __call__(self, context, configuration, directory, cwd):
        self.calls.append((copy.deepcopy(context), copy.deepcopy(configuration), Path(cwd)))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        run_dir = Path(directory) / 'artifact'
        run_dir.mkdir(parents=True, exist_ok=True)
        events = [{'type': 'turn.completed', 'usage': {'input_tokens': 11, 'cached_input_tokens': 4, 'output_tokens': 3}}] if self.usage else []
        (run_dir / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        return copy.deepcopy(value)


PLAN = {'decision': 'plan', 'summary': 'p', 'plan': 'Do the thing.', 'acceptance': ['Works'], 'questions': []}
ASK = {'decision': 'questions', 'summary': 'q', 'plan': '', 'acceptance': [], 'questions': ['Which format?']}


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def plan(self, task, treatment, responses, usage=True):
        assess = FakeAssess(responses, usage)
        record = evaluate.plan_task(manifest(), tasks()[task], treatment, self.root / f'{task}-{treatment}', self.root, assess=assess)
        return record, assess

    def test_a_plan_is_the_canonical_document_for_either_treatment(self):
        for treatment in ('control', 'strengthened'):
            record, assess = self.plan('list-limit', treatment, [PLAN])
            self.assertEqual('planned', record['status'])
            self.assertEqual(specification.render('Do the thing.', ['Works']), record['document'])
            self.assertEqual(('reported', 11), (record['usage']['status'], record['usage']['input_tokens']))
            self.assertEqual({'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}, assess.calls[0][1]['processing'])

    def test_both_arms_see_the_same_planning_context(self):
        _, control = self.plan('list-limit', 'control', [PLAN])
        _, strengthened = self.plan('list-limit', 'strengthened', [PLAN])
        self.assertEqual(control.calls[0][:2], strengthened.calls[0][:2])
        self.assertEqual('Add --limit to `ledger list`', control.calls[0][0]['issue']['title'])
        self.assertIsNone(control.calls[0][0]['previous_plan'])

    def test_the_control_formula_equals_the_shared_document_renderer(self):
        text = "Plan\n\nAcceptance criteria:\n- A\n- B"
        self.assertEqual(text, specification.render('Plan', ['A', 'B']))
        legacy = 'Plan' + '\n\nAcceptance criteria:\n' + '\n'.join('- ' + a for a in ['A', 'B'])
        self.assertEqual(legacy, specification.render('Plan', ['A', 'B']))

    def test_predetermined_answers_are_supplied_identically_to_both_arms(self):
        records = {}
        for treatment in ('control', 'strengthened'):
            records[treatment], assess = self.plan('export', treatment, [ASK, PLAN])
            self.assertEqual(2, len(assess.calls))
            self.assertEqual([], assess.calls[0][0]['comments'])
            self.assertEqual(tasks()['export']['answers'], assess.calls[1][0]['comments'][0]['body'])
            self.assertEqual('maintainer', assess.calls[1][0]['comments'][0]['user']['login'])
            self.assertEqual(1, records[treatment]['clarification_turns'])
        self.assertEqual(records['control']['comments'], records['strengthened']['comments'])

    def test_answers_are_never_pushed_to_an_arm_that_does_not_ask(self):
        record, assess = self.plan('export', 'control', [PLAN])
        self.assertEqual(0, record['clarification_turns'])
        self.assertFalse(record['answers_supplied'])
        self.assertEqual([[]], [c[0]['comments'] for c in assess.calls])

    def test_unanswerable_or_repeated_questions_stop_planning_and_are_retained(self):
        record, _ = self.plan('list-limit', 'strengthened', [ASK])
        self.assertEqual('unanswered_questions', record['status'])
        self.assertEqual([['Which format?']], record['questions'])
        record, _ = self.plan('export', 'strengthened', [ASK, ASK])
        self.assertEqual('unanswered_questions', record['status'])
        self.assertEqual(1, record['clarification_turns'])
        self.assertEqual(2, len(record['turns']))

    def test_planning_failures_keep_their_error_time_and_usage_status(self):
        record, _ = self.plan('list-limit', 'strengthened', [ApiError('invalid_result', 'Invalid decisions')], usage=False)
        self.assertEqual('planning_failed', record['status'])
        self.assertIn('invalid_result', record['turns'][0]['error'])
        self.assertEqual('unavailable', record['usage']['status'])
        self.assertIsNone(record['document'])

    def test_terminal_decisions_are_not_plans(self):
        record, _ = self.plan('list-limit', 'control', [{**PLAN, 'decision': 'no_change', 'plan': '', 'acceptance': []}])
        self.assertEqual('no_plan_no_change', record['status'])


class Provider:
    """Scripted provider boundary: replies by role, records every prompt and specification handoff."""
    def __init__(self, implement, review=(), usage=True):
        self.implement, self.review, self.usage = list(implement), list(review), usage
        self.calls = []

    def __call__(self, choice, prompt, directory, cwd, schema, conversation, timeout, runner=None, role='fix'):
        queue = self.review if role == 'review' else self.implement
        script = queue.pop(0)
        context = json.loads(prompt[prompt.index('{'):])
        handle = context['approved_specification']
        self.calls.append({'role': role, 'choice': dict(choice), 'file': Path(handle['file']).read_bytes() if Path(handle['file']).exists() else None,
                           'handle': handle, 'cwd': Path(cwd), 'feedback': context.get('feedback'), 'conversation': dict(conversation),
                           'timeout': timeout, 'prompt': prompt})
        if isinstance(script, BaseException):
            return {'status': 'interrupted', 'error': str(script), 'elapsed_seconds': 1.0,
                    'usage': {'status': 'unavailable'}, 'session_id': 'S' if role == 'fix' else None}
        if role == 'fix' and script.get('write'):
            for name, text in script['write'].items():
                (Path(cwd) / name).write_text(text)
        usage = ({'status': 'reported', 'turns_reported': 1, 'input_tokens': 10, 'cached_input_tokens': 2, 'output_tokens': 1,
                  'reasoning_output_tokens': 0, 'provider_cost_usd': 'unavailable'} if self.usage else {'status': 'unavailable'})
        return {'status': 'ok', 'value': script['value'], 'elapsed_seconds': 2.0, 'usage': usage,
                'session_id': 'S' if role == 'fix' else None}


def report(status='ready', **fields):
    return {'value': {'status': status, 'summary': 'Candidate', 'checks': ['t'], 'findings': fields.get('findings', []),
                      'questions': fields.get('questions', [])}}


def verdict(assessment='passed', findings=()):
    return {'value': {'assessment': assessment, 'summary': 'review', 'findings': list(findings)}}


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.m = manifest()
        self.checks = lambda m, task, checkout: {'acceptance': {n: True for n in task['acceptance']},
                                                  'regression': {n: True for n in task['regression']}}

    def planning(self, task, document, **fields):
        return {'treatment': 'x', 'task': task, 'status': 'planned', 'document': document, 'elapsed_seconds': 4.0,
                'usage': {'status': 'reported', 'calls': 1, 'calls_unavailable': 0, 'input_tokens': 5, 'cached_input_tokens': 0,
                          'output_tokens': 1, 'reasoning_output_tokens': 0, 'provider_cost_usd': 'unavailable'},
                'clarification_turns': 0, 'comments': [], 'turns': [], **fields}

    def run_for(self, arm, config, provider, task='list-limit', planning=None, checks=None):
        run = next(r for r in evaluate.matrix(self.m) if r['task'] == task and r['config'] == config and r['arm'] == arm)
        task_value = evaluate.load_task(task)
        planning = planning or self.planning(task, specification.render(f'{arm} plan with Unicode ✓', ['Works']))
        return evaluate.execute_run(self.m, run, task_value, planning, self.root / run['id'], provider, checks or self.checks)

    def test_both_arms_get_byte_identical_lossless_transport_and_identical_settings(self):
        results, providers = {}, {}
        document = specification.render('Shared plan ✓\r\nwith unicode', ['Works'])
        for arm in ('control', 'strengthened'):
            providers[arm] = Provider([report()], [verdict()])
            results[arm] = self.run_for(arm, 'economical', providers[arm], planning=self.planning('list-limit', document))
        for arm, provider in providers.items():
            first = provider.calls[0]
            self.assertEqual(document.encode('utf-8'), first['file'])
            self.assertEqual(evaluate.sha256(document.encode('utf-8')), first['handle']['sha256'])
        self.assertEqual(providers['control'].calls[0]['handle']['sha256'], providers['strengthened'].calls[0]['handle']['sha256'])
        self.assertEqual(results['control']['settings'], results['strengthened']['settings'])
        for field in ('choice', 'timeout'):
            self.assertEqual([c[field] for c in providers['control'].calls], [c[field] for c in providers['strengthened'].calls])
        self.assertEqual({'provider': 'codex', 'model': 'gpt-6.1-terra', 'effort': 'low'}, providers['control'].calls[0]['choice'])
        self.assertEqual(self.m['review'], providers['control'].calls[1]['choice'])

    def test_the_default_configuration_is_used_for_default_runs_and_review_never_varies(self):
        economical, default = Provider([report()], [verdict()]), Provider([report()], [verdict()])
        self.run_for('control', 'economical', economical)
        self.run_for('control', 'default', default)
        self.assertEqual('gpt-6.1-terra', economical.calls[0]['choice']['model'])
        self.assertEqual('gpt-6.1-sol', default.calls[0]['choice']['model'])
        self.assertEqual(economical.calls[1]['choice'], default.calls[1]['choice'])

    def test_prompts_differ_only_by_the_handed_specification_location(self):
        prompts = {}
        document = specification.render('SECRET-SPEC-TEXT ✓', ['Works'])
        for arm in ('control', 'strengthened'):
            provider = Provider([report()], [verdict()])
            self.run_for(arm, 'economical', provider, planning=self.planning('list-limit', document))
            prompts[arm] = re.sub(r'/[^"]*/revision-1\.md', '<FILE>', re.sub(r'/[^"]*/repo', '<REPO>', provider.calls[0]['prompt']))
            prompts[arm] = re.sub(r'/[^"]*scratch', '<SCRATCH>', prompts[arm])
        self.assertEqual(prompts['control'], prompts['strengthened'])
        self.assertNotIn('SECRET-SPEC-TEXT', prompts['control'])    # the specification itself is read from the file

    def test_repair_rounds_follow_reviewer_findings_and_resume_the_same_session(self):
        provider = Provider([report(), report(), report()],
                            [verdict('changes_requested', ['limit 0 accepted']), verdict('changes_requested', ['help text']), verdict()])
        result = self.run_for('control', 'economical', provider)
        self.assertEqual('completed', result['status'])
        self.assertEqual(2, result['implementation']['repair_rounds'])
        self.assertEqual(['limit 0 accepted'], provider.calls[2]['feedback'])
        self.assertEqual('S', provider.calls[2]['conversation']['session_id'])
        self.assertEqual(3, result['implementation']['review_rounds'])

    def test_repair_limit_is_enforced_and_the_run_is_failed_but_retained(self):
        provider = Provider([report()] * 4, [verdict('changes_requested', ['x'])] * 4)
        result = self.run_for('control', 'economical', provider)
        self.assertEqual('failed', result['status'])
        self.assertEqual('max_repair_rounds', result['implementation']['stop'])
        self.assertEqual(self.m['limits']['max_repair_rounds'], result['implementation']['repair_rounds'])
        self.assertTrue(result['score']['correct'])           # independent checks decide correctness, not the reviewer
        self.assertIsNotNone(result['checks'])

    def test_interruptions_and_failed_checks_are_retained_with_unavailable_usage(self):
        provider = Provider([RuntimeError('timeout')], [])
        crashed = lambda m, task, checkout: None
        result = self.run_for('strengthened', 'default', provider, checks=crashed)
        self.assertEqual('failed', result['status'])
        self.assertEqual('provider_interrupted', result['implementation']['stop'])
        self.assertEqual('unavailable', result['implementation']['usage']['status'])
        self.assertFalse(result['score']['correct'])
        self.assertFalse(result['score']['checks_ran'])
        self.assertEqual('partial', result['end_to_end']['usage']['status'])    # planning reported, implementation did not

    def test_working_reports_continue_without_review_and_bad_reports_get_one_correction(self):
        provider = Provider([report('working'), {'value': {'status': 'ready'}}, report()], [verdict()])
        result = self.run_for('control', 'economical', provider)
        self.assertEqual('completed', result['status'])
        self.assertEqual(1, result['implementation']['continuations'])
        self.assertEqual(1, result['implementation']['format_corrections'])
        self.assertEqual(1, result['implementation']['review_rounds'])
        self.assertEqual(4 - 1, len([c for c in provider.calls if c['role'] == 'fix']))

    def test_product_questions_receive_the_same_predetermined_answers_once(self):
        answers = evaluate.load_task('export')['answers']
        outcomes = []
        for arm in ('control', 'strengthened'):
            provider = Provider([report('needs_input', questions=['Overwrite?']), report()], [verdict()])
            result = self.run_for(arm, 'economical', provider, task='export')
            outcomes.append((result['implementation']['clarification_turns'], provider.calls[1]['feedback']))
            self.assertEqual('completed', result['status'])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertIn(answers, outcomes[0][1][0])
        silent = Provider([report('needs_input', questions=['Which?'])], [])
        result = self.run_for('control', 'economical', silent)          # no predetermined answers for this task
        self.assertEqual('needs_input', result['implementation']['stop'])
        self.assertEqual('failed', result['status'])

    def test_a_failed_plan_is_a_failed_run_with_no_implementation_not_a_dropped_one(self):
        planning = self.planning('list-limit', None, status='unanswered_questions', usage={'status': 'unavailable'})
        provider = Provider([], [])
        result = self.run_for('control', 'economical', provider, planning=planning)
        self.assertEqual('failed', result['status'])
        self.assertEqual('not_started', result['implementation']['status'])
        self.assertEqual('unanswered_questions', result['implementation']['stop'])
        self.assertEqual(0.0, result['score']['acceptance_coverage'])
        self.assertEqual([], provider.calls)

    def test_the_model_workspace_never_receives_hidden_check_paths(self):
        provider = Provider([report()], [verdict()])
        self.run_for('control', 'economical', provider)
        for call in provider.calls:
            self.assertNotIn(str(EVAL / 'checks'), call['prompt'])
            self.assertNotIn('LEDGER_ORIGINAL_TESTS', call['prompt'])
        workspace = self.root / 'list-limit.economical.control' / 'repo'
        self.assertFalse((workspace / 'checks').exists())

    def test_no_controller_pipeline_features_exist_in_the_runner(self):
        source = (EVAL / 'evaluate.py').read_text()
        for forbidden in ('per_task_approval', 'task_ledger', 'checkpoint_approval'):
            self.assertNotIn(forbidden, source)


class InterruptionAndResumeTest(unittest.TestCase):
    """Interrupted work is retained, and the documented re-run resumes instead of crashing."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.m = manifest()
        self.m['tasks'] = ['list-limit']

    def fake_plan(self, document='Plan with Unicode \u2713 and /home/user/path\n'):
        def plan(manifest_, task, arm, target, checkout):
            Path(target).mkdir(parents=True, exist_ok=True)
            return {'treatment': arm, 'task': task['id'], 'status': 'planned', 'document': document,
                    'document_sha256': evaluate.sha256(document.encode('utf-8')), 'elapsed_seconds': 1.0,
                    'usage': {'status': 'unavailable'}, 'clarification_turns': 0, 'comments': [], 'turns': []}
        return plan

    def result(self, run, status):
        task = evaluate.load_task(run['task'])
        return {'id': run['id'], 'status': status, 'score': evaluate.score(task, None), 'implementation': {}, 'planning': {},
                'end_to_end': {'elapsed_seconds': 0, 'usage': {'status': 'unavailable'}}}

    def test_an_interrupted_run_is_written_and_the_rerun_retains_it_and_continues(self):
        out = self.root / 'out'
        calls = []

        def interrupt_first(manifest_, run, task, planning, directory):
            calls.append(run['id'])
            return self.result(run, 'interrupted' if len(calls) == 1 else 'completed')
        self.assertEqual(130, evaluate.run_matrix(self.m, out, plan=self.fake_plan(), execute=interrupt_first))
        first = evaluate.matrix(self.m)[0]['id']
        written = json.loads((out / 'runs' / first / 'result.json').read_text())
        self.assertEqual('interrupted', written['status'])
        self.assertEqual([first], calls)
        # Summaries count it as a failed, not a missing, run.
        summary = evaluate.summarize(self.m, [dict(written, pair=evaluate.matrix(self.m)[0]['pair'], planning={'elapsed_seconds': 0},
                                                     implementation={'repair_rounds': 0, 'elapsed_seconds': 0})])
        self.assertEqual(1, sum(a['failed'] for a in summary['arms'].values()))
        self.assertEqual(0, evaluate.run_matrix(self.m, out, plan=self.fake_plan(), execute=interrupt_first))
        directory = out / 'runs' / first
        self.assertEqual('interrupted', json.loads((directory / 'result.interrupted-1.json').read_text())['status'])
        self.assertEqual('completed', json.loads((directory / 'result.json').read_text())['status'])
        self.assertEqual(len(evaluate.matrix(self.m)), len({c for c in calls}))
        calls.clear()
        self.assertEqual(0, evaluate.run_matrix(self.m, out, plan=self.fake_plan(), execute=interrupt_first))
        self.assertEqual([], calls)                 # completed cells are never repeated

    def test_an_interrupted_plan_is_retained_and_replanned_on_resume(self):
        out = self.root / 'out'

        def interrupted_plan(manifest_, task, arm, target, checkout):
            Path(target).mkdir(parents=True, exist_ok=True)
            return {'treatment': arm, 'task': task['id'], 'status': 'planning_interrupted', 'document': None, 'turns': [],
                    'elapsed_seconds': 2.0, 'usage': {'status': 'unavailable'}}
        executed = []
        self.assertEqual(130, evaluate.run_matrix(self.m, out, plan=interrupted_plan, execute=lambda *a: executed.append(a)))
        self.assertEqual([], executed)
        planning = next((out / 'planning').iterdir())
        self.assertEqual('planning_interrupted', json.loads((planning / 'planning.json').read_text())['status'])
        self.assertEqual(0, evaluate.run_matrix(self.m, out, plan=self.fake_plan(),
                                                execute=lambda m_, run, *a: self.result(run, 'completed')))
        self.assertTrue(any(planning.glob('planning.interrupted-1.json')))
        self.assertEqual('planned', json.loads((planning / 'planning.json').read_text())['status'])

    def test_a_leftover_workspace_does_not_stop_the_rerun(self):
        m = manifest()
        destination = self.root / 'work' / 'repo'
        evaluate.baseline_checkout(m, destination)
        (destination / 'half-done.txt').write_text('partial model output')
        evaluate.baseline_checkout(m, destination)             # used to raise FileExistsError
        self.assertFalse((destination / 'half-done.txt').exists())
        self.assertTrue((destination / 'ledger' / 'cli.py').exists())

    def test_resumed_planning_reloads_the_handed_bytes_not_the_sanitized_record(self):
        out = self.root / 'out'
        document = 'Keep /home/user/path and \u2713 exactly\n'
        home = str(Path.home())
        seen = []
        evaluate.run_matrix(self.m, out, plan=self.fake_plan(document.replace('/home/user', home)),
                            execute=lambda m_, run, task, planning, directory: (seen.append(planning['document']), self.result(run, 'completed'))[1])
        saved = next((out / 'planning').iterdir())
        self.assertNotIn('"document"', (saved / 'planning.json').read_text())
        shutil.rmtree(out / 'runs')
        seen.clear()
        evaluate.run_matrix(self.m, out, plan=self.fake_plan('unused'),
                            execute=lambda m_, run, task, planning, directory: (seen.append(planning['document']), self.result(run, 'completed'))[1])
        self.assertEqual({document.replace('/home/user', home)}, set(seen))
        (saved / 'specification.md').write_bytes(b'edited')
        with self.assertRaises(SystemExit):
            evaluate.run_matrix(self.m, out, plan=self.fake_plan('unused'), execute=lambda *a: None)

    def test_a_keyboard_interrupt_in_a_provider_call_yields_a_retained_scored_result(self):
        m = manifest()
        task = evaluate.load_task('list-limit')
        run = next(r for r in evaluate.matrix(m) if r['task'] == 'list-limit' and r['config'] == 'economical' and r['arm'] == 'control')
        document = specification.render('Plan', ['Works'])
        planning = {'status': 'planned', 'document': document, 'elapsed_seconds': 1.0, 'comments': [], 'clarification_turns': 0,
                    'usage': {'status': 'reported', 'calls': 1, 'calls_unavailable': 0, 'input_tokens': 1, 'cached_input_tokens': 0,
                              'output_tokens': 1, 'reasoning_output_tokens': 0, 'provider_cost_usd': 'unavailable'}}

        def provider(*args, **kwargs):
            error = KeyboardInterrupt()
            error.call_record = {'status': 'interrupted', 'kind': 'implementation', 'elapsed_seconds': 3.0,
                                 'usage': {'status': 'reported', 'turns_reported': 1, 'input_tokens': 7, 'cached_input_tokens': 0,
                                           'output_tokens': 1, 'reasoning_output_tokens': 0, 'provider_cost_usd': 'unavailable'}}
            raise error
        checked = []
        result = evaluate.execute_run(m, run, task, planning, self.root / 'run', provider,
                                      lambda m_, t, checkout: (checked.append(checkout), None)[1])
        self.assertEqual('interrupted', result['status'])
        self.assertEqual('interrupted: KeyboardInterrupt', result['implementation']['stop'])
        self.assertEqual(7, result['implementation']['usage']['input_tokens'])
        self.assertEqual(1, len(result['implementation']['calls']))
        self.assertEqual(1, len(checked))                        # the partial tree is still scored
        self.assertFalse(result['score']['correct'])

    def test_a_keyboard_interrupt_while_planning_is_marked_not_raised(self):
        def interrupted(context, configuration, directory, cwd):
            raise KeyboardInterrupt()
        record = evaluate.plan_task(manifest(), tasks()['list-limit'], 'control', self.root / 'plan', self.root, assess=interrupted)
        self.assertEqual('planning_interrupted', record['status'])
        self.assertEqual('interrupted', record['turns'][0]['status'])
        self.assertEqual('unavailable', record['usage']['status'])


class ExportResultsTest(unittest.TestCase):
    def test_candidate_diffs_are_written_and_a_git_failure_is_never_silent(self):
        import subprocess
        spec = importlib.util.spec_from_file_location('export_results_module', EVAL / 'export_results.py')
        export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(export)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'work' / 'task.cfg.control' / 'repo'
            evaluate.baseline_checkout(manifest(), repo)
            (repo / 'ledger' / 'cli.py').write_text((repo / 'ledger' / 'cli.py').read_text() + '# changed\n')
            (repo / 'ledger' / 'new_module.py').write_text('NEW = 1\n')
            (repo / 'ledger' / '__pycache__').mkdir()
            (repo / 'ledger' / '__pycache__' / 'x.pyc').write_bytes(b'0')
            (root / 'runs' / 'task.cfg.control').mkdir(parents=True)
            (root / 'runs' / 'task.cfg.control' / 'result.json').write_text('{}')
            (root / 'planning').mkdir()
            export.main(str(root), str(root / 'exported'))
            diff = (root / 'exported' / 'runs' / 'task.cfg.control' / 'changes.diff').read_text()
            self.assertIn('+# changed', diff)
            self.assertIn('new_module.py', diff)
            self.assertNotIn('__pycache__', diff)
            shutil.rmtree(repo / '.git')                         # git now fails: the export must fail loudly
            with self.assertRaises(subprocess.CalledProcessError):
                export.main(str(root), str(root / 'exported-again'))


class OriginalTestsTargetTheCandidateTest(unittest.TestCase):
    """The pristine regression tests must exercise the candidate package, not the frozen baseline."""
    def checks(self, mutate, task='list-limit'):
        m = manifest()
        with tempfile.TemporaryDirectory() as scratch:
            checkout = Path(scratch) / 'candidate'
            evaluate.baseline_checkout(m, checkout)
            mutate(checkout)
            return evaluate.run_checks(m, evaluate.load_task(task), checkout)

    def test_a_candidate_that_breaks_existing_behavior_fails_the_original_tests(self):
        def break_store(checkout):
            with (checkout / 'ledger' / 'store.py').open('a') as stream:
                stream.write("\nraise RuntimeError('broken candidate')\n")
        output = self.checks(break_store)
        self.assertFalse(output['regression']['original_tests_pass'])
        self.assertFalse(output['regression']['repository_tests_pass'])

    def test_deleting_or_weakening_the_candidates_own_tests_cannot_hide_a_regression(self):
        def break_and_remove_tests(checkout):
            with (checkout / 'ledger' / 'report.py').open('a') as stream:
                stream.write("\n\ndef format_cents(cents):\n    return 'wrong'\n")
            shutil.rmtree(checkout / 'tests')
        output = self.checks(break_and_remove_tests)
        self.assertFalse(output['regression']['original_tests_pass'])

    def test_an_unchanged_candidate_passes_and_the_candidate_is_the_imported_package(self):
        self.assertTrue(self.checks(lambda checkout: None)['regression']['original_tests_pass'])

    def test_retained_diffs_can_be_rescored_from_a_fresh_baseline(self):
        m = manifest()
        task = evaluate.load_task('category-case')
        with tempfile.TemporaryDirectory() as scratch:
            checkout = Path(scratch) / 'reference'
            evaluate.baseline_checkout(m, checkout)
            shutil.copytree(EVAL / 'reference' / 'category-case', checkout, dirs_exist_ok=True)
            import subprocess
            subprocess.run(['git', 'add', '-A', '-N'], cwd=checkout, check=True, capture_output=True)
            diff = subprocess.run(['git', 'diff', 'HEAD'], cwd=checkout, check=True, capture_output=True, text=True).stdout
            self.assertIn('ledger/report.py', diff)
            self.assertTrue(evaluate.score(task, evaluate.rescore_diff(m, task, diff, Path(scratch) / 'a'))['correct'])
            self.assertFalse(evaluate.score(task, evaluate.rescore_diff(m, task, '', Path(scratch) / 'b'))['correct'])


class IndependentChecksTest(unittest.TestCase):
    def test_every_check_set_fails_the_baseline_and_passes_a_known_correct_reference(self):
        m = manifest()
        for name in m['tasks']:
            task = evaluate.load_task(name)
            with tempfile.TemporaryDirectory() as scratch:
                base = Path(scratch) / 'base'
                evaluate.baseline_checkout(m, base)
                before = evaluate.score(task, evaluate.run_checks(m, task, base))
                reference = Path(scratch) / 'reference'
                evaluate.baseline_checkout(m, reference)
                shutil.copytree(EVAL / 'reference' / name, reference, dirs_exist_ok=True)
                after = evaluate.score(task, evaluate.run_checks(m, task, reference))
            with self.subTest(task=name):
                self.assertTrue(after['correct'], after)
                self.assertFalse(before['correct'])
                self.assertLess(before['acceptance_passed'], before['acceptance_total'])


if __name__ == '__main__':
    unittest.main()
