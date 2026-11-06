"""Usage accounting with scripted processes and provider telemetry fixtures; no inference."""
import copy
import json
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import usage, usage_quota
from pr_review.common import ApiError, read_json
from pr_review.events import event_sink
from pr_review.processes import Cancelled, cancellation_scope, run
from pr_review.storage import write_json


def reading(used=10, reset=100000, scoped=None):
    meters = [{'id': 'all', 'label': 'Provider weekly, all models', 'kind': 'weekly_all',
               'scope': None, 'used': used, 'reset': reset}]
    if scoped is not None:
        meters.append(dict(meters[0], id='fable', label='Anthropic weekly, Fable', kind='weekly_scoped',
                           scope={'model': 'Fable'}, used=scoped))
    return {'status': 'reported', 'meters': meters}


def codex(incoming=100, cached=60, outgoing=10):
    return [{'type': 'thread.started', 'thread_id': 'thread-1'},
            {'type': 'turn.completed', 'turn_id': 'turn-1',
             'usage': {'input_tokens': incoming, 'cached_input_tokens': cached,
                       'output_tokens': outgoing, 'reasoning_output_tokens': 3}}]


def claude(incoming=100, *, sid='session-1', subagent=20):
    return [{'type': 'system', 'subtype': 'init', 'session_id': sid, 'claude_code_version': '2.1.318'},
            {'type': 'result', 'uuid': 'result-' + str(incoming), 'session_id': sid,
             'subtype': 'success', 'is_error': False,
             'usage': {'input_tokens': incoming, 'output_tokens': 10},
             'modelUsage': {'fable': {'inputTokens': incoming, 'outputTokens': 10,
                                     'cacheReadInputTokens': 50, 'cacheCreationInputTokens': 5},
                            'haiku': {'inputTokens': subagent, 'outputTokens': 2}}}]


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'job'
        self.messages = []
        sink = event_sink(lambda kind, **fields: self.messages.append(fields.get('message', '')))
        sink.__enter__()
        self.addCleanup(sink.__exit__, None, None, None)
        self.telemetry = patch('pr_review.usage_quota.read', return_value=reading())
        self.probe = self.telemetry.start()
        self.addCleanup(self.telemetry.stop)

    def call(self, events=None, provider='codex', role='fix', effort='high', resumed=False, code=0):
        descriptor = usage.choice(role, {'provider': provider, 'model': 'fable' if provider == 'claude' else 'sol', 'effort': effort})
        descriptor['resumed'] = resumed
        program = 'import sys; print(' + repr('\n'.join(json.dumps(e) for e in (events if events is not None else codex()))) + '); sys.exit(' + str(code) + ')'
        return run([sys.executable, '-c', program], model_events=True, usage_call=descriptor)

    def report(self):
        return usage.summary(self.root)

    def test_resumes_keep_totals_and_exclude_feedback_wait_quota(self):
        self.probe.side_effect = [reading(10), reading(12), reading(60), reading(63)]
        with usage.operation(self.root, legacy=False):
            self.call()
        with usage.operation(self.root):
            self.call()
        value = self.report()
        self.assertEqual(2, value['calls'])
        self.assertEqual(220, value['roles'][0]['usage']['total_tokens'])
        self.assertEqual(5, value['quota'][0]['consumed_points'])
        self.assertFalse(value['historical_usage_unknown'])
        self.assertEqual(2, len(read_json(self.root / 'usage/ledger.json')['invocations']))
        self.assertTrue(any('Model' in line and 'Effort' in line for line in self.messages))

    def test_calls_split_by_effective_role_model_and_effort(self):
        with usage.operation(self.root, legacy=False):
            self.call(role='planning')
            self.call(role='fix', effort='medium')
            self.call(role='fix', effort='high')
            self.call(role='final-review/reviewer-1')
        value = self.report()
        self.assertEqual(4, len(value['roles']))
        self.assertEqual(['planning', 'fix', 'fix', 'final-review/reviewer-1'], [r['role'] for r in value['roles']])
        self.assertEqual(['high', 'medium', 'high', 'high'], [r['effort'] for r in value['roles']])
        self.assertEqual(8, self.probe.call_count)

    def test_metadata_reads_ordinary_processes_and_cache_reuse_add_no_calls(self):
        with usage.operation(self.root, legacy=False):
            self.call()
            run([sys.executable, '-c', 'print(1)'])
        self.probe.reset_mock()
        with usage.operation(self.root):
            # A cache hit does not invoke the process runner.
            self.assertEqual(1, self.report()['calls'])
        self.report()
        self.probe.assert_not_called()

    def test_nested_owner_review_uses_issue_ledger_without_duplicate_counts(self):
        with usage.operation(self.root, legacy=False):
            self.call(role='planning')
            with usage.operation(self.root / 'owner'):
                self.call(role='final-review/synthesis')
        self.assertEqual(2, self.report()['calls'])
        self.assertFalse((self.root / 'owner/usage/ledger.json').exists())
        owner = type('Owner', (), {'path': self.root / 'owner'})()
        self.assertEqual(self.root, usage.owner_root(owner, {'usage_root': str(self.root)}))
        self.assertEqual(owner.path, usage.owner_root(owner, {'issue_workflow': 'workflow-id-not-a-path'}))

    def test_separate_job_roots_do_not_merge_on_provider_session_or_role(self):
        for root in (self.root, self.root.parent / 'other-job'):
            with usage.operation(root, legacy=False):
                self.call()
        self.assertEqual(1, self.report()['calls'])

    def test_claude_session_deltas_include_subagents_and_do_not_add_top_level_usage(self):
        with usage.operation(self.root, legacy=False):
            self.call(claude(100), provider='claude')
        with usage.operation(self.root):
            self.call(claude(140, subagent=30), provider='claude', resumed=True)
        role = self.report()['roles'][0]
        self.assertEqual(237, role['usage']['total_tokens'])
        self.assertEqual(205, role['model_usage']['fable']['total_tokens'])
        self.assertEqual(32, role['model_usage']['haiku']['total_tokens'])
        self.assertEqual('reported', role['coverage'])

    def test_duplicate_claude_results_are_cumulative_not_summed(self):
        events = claude()
        events.append(copy.deepcopy(events[-1]))
        with usage.operation(self.root, legacy=False):
            self.call(events, provider='claude')
        self.assertEqual(187, self.report()['roles'][0]['usage']['total_tokens'])

    def test_streamed_steps_are_deduplicated_and_replaced_by_result(self):
        assistant = {'type': 'assistant', 'message': {'id': 'same-id', 'usage': {'input_tokens': 100, 'output_tokens': 10}}}
        with usage.operation(self.root, legacy=False):
            self.call([*claude()[:1], assistant, assistant, claude()[-1]], provider='claude')
        self.assertEqual(187, self.report()['roles'][0]['usage']['total_tokens'])

    def test_legacy_claude_result_is_marked_main_loop_only(self):
        events = claude()
        del events[-1]['modelUsage']
        with usage.operation(self.root, legacy=False):
            self.call(events, provider='claude')
        role = self.report()['roles'][0]
        self.assertEqual(110, role['usage']['total_tokens'])
        self.assertEqual('partial', role['coverage'])

    def test_preexisting_resumed_claude_session_does_not_charge_restored_history(self):
        with usage.operation(self.root, legacy=True):
            self.call(claude(1000), provider='claude', resumed=True)
        self.assertIsNone(self.report()['roles'][0]['usage'])
        with usage.operation(self.root):
            self.call(claude(1040), provider='claude', resumed=True)
        role = self.report()['roles'][0]
        self.assertEqual(40, role['usage']['total_tokens'])
        self.assertEqual('partial', role['coverage'])
        self.assertTrue(self.report()['historical_usage_unknown'])

    def test_interrupted_main_loop_usage_remains_partial_and_next_resume_avoids_double_count(self):
        assistant = {'type': 'assistant', 'session_id': 'session-1', 'message': {
            'id': 'm1', 'usage': {'input_tokens': 10, 'output_tokens': 3}}}
        with usage.operation(self.root, legacy=False):
            self.call([assistant, assistant], provider='claude', code=2)
        self.assertEqual(13, self.report()['roles'][0]['usage']['total_tokens'])
        with usage.operation(self.root):
            self.call(claude(100), provider='claude', resumed=True)
        self.assertEqual(13, self.report()['roles'][0]['usage']['total_tokens'])
        with usage.operation(self.root):
            self.call(claude(110), provider='claude', resumed=True)
        self.assertEqual(23, self.report()['roles'][0]['usage']['total_tokens'])

    def test_successful_zero_usage_differs_from_missing_usage(self):
        with usage.operation(self.root, legacy=False):
            self.call(codex(0, 0, 0), role='auth-probe/codex')
            self.call([{'type': 'turn.completed'}])
        zero, missing = self.report()['roles']
        self.assertEqual(0, zero['usage']['total_tokens'])
        self.assertIsNone(missing['usage'])
        self.assertEqual('partial', self.report()['coverage'])

    def test_codex_zero_counters_after_model_activity_are_unavailable_across_resumes(self):
        # exec review can execute tools and produce a report, yet finish with zero counters.
        events = [codex()[0], {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'pwd'}},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'No defects found.'}},
                  codex(0, 0, 0)[-1]]
        with usage.operation(self.root, legacy=False):
            self.assertEqual(0, self.call(events, role='reviewer-1').returncode)
        value = self.report()
        role = value['roles'][0]
        self.assertIsNone(role['usage'])
        self.assertEqual('unavailable', role['token_coverage'])
        self.assertEqual('reported', role['quota_coverage'])
        self.assertEqual('partial', value['coverage'])
        self.assertIn('zero tokens despite model activity', role['token_warnings'][0])
        self.assertTrue(any('tokens unavailable' in m for m in self.messages))
        self.assertTrue(any('Warning:' in m and 'zero tokens' in m for m in self.messages))
        with usage.operation(self.root):
            self.call(role='reviewer-1')
        role = self.report()['roles'][0]
        self.assertEqual((2, 1, 1), (role['calls'], role['calls_reported'], role['calls_unavailable']))
        self.assertEqual(110, role['usage']['total_tokens'])
        self.assertEqual('partial', role['token_coverage'])
        self.assertEqual(1, len(role['token_warnings']))

    def test_codex_activity_requires_counters_but_does_not_invalidate_positive_usage(self):
        for item in ({'type': 'agent_message', 'text': 'A finding'},
                     {'type': 'command_execution', 'command': 'pwd'},
                     {'type': 'mcp_tool_call', 'tool': 'read_file'},
                     {'type': 'reasoning', 'text': 'Inspect the diff'}):
            with self.subTest(item=item['type']), usage.operation(self.root, legacy=False):
                activity = {'type': 'item.completed', 'item': item}
                self.call([codex()[0], activity, codex(0, 0, 0)[-1]], role=item['type'] + '/zero')
                self.call([codex()[0], activity, codex()[-1]], role=item['type'] + '/positive')
        for zero, positive in zip(self.report()['roles'][::2], self.report()['roles'][1::2]):
            self.assertIsNone(zero['usage'])
            self.assertEqual(110, positive['usage']['total_tokens'])
            self.assertEqual('reported', positive['token_coverage'])
            self.assertEqual([], positive['token_warnings'])

    def test_codex_missing_and_suspicious_turns_preserve_known_usage_as_partial(self):
        positive = codex()[-1]
        for incomplete in ({'type': 'turn.completed', 'turn_id': 'turn-2'},
                           dict(codex(0, 0, 0)[-1], turn_id='turn-2')):
            for order in ('before', 'after'):
                with self.subTest(incomplete=incomplete, order=order):
                    activity = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Report'}}
                    turns = [activity, incomplete, positive] if order == 'before' else [positive, activity, incomplete]
                    role_name = str(len(self.report()['roles']))
                    with usage.operation(self.root, legacy=False):
                        self.call([codex()[0], *turns], role=role_name)
                    role = self.report()['roles'][-1]
                    self.assertEqual(110, role['usage']['total_tokens'])
                    self.assertEqual('partial', role['token_coverage'])

    def test_duplicate_suspicious_codex_completion_does_not_restore_reported_coverage(self):
        activity = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Report'}}
        zero = codex(0, 0, 0)[-1]
        with usage.operation(self.root, legacy=False):
            self.call([codex()[0], activity, zero, zero])
        self.assertIsNone(self.report()['roles'][0]['usage'])
        self.assertEqual('unavailable', self.report()['token_coverage'])

    def test_usage_tables_distinguish_observed_quota_and_cached_tokens(self):
        with usage.operation(self.root, legacy=False):
            self.call()
        text = '\n'.join(self.messages)
        self.assertIn('Observed change (pp)', text)
        self.assertIn('Weekly change (pp)', text)
        self.assertIn('Token coverage', text)
        self.assertIn('Quota coverage', text)
        self.assertIn('Cache read', text)
        self.assertIn('Cache read is a subset of Reported tokens', text)
        self.assertIn('Zero quota change means unchanged readings', text)
        row = next(line for line in text.splitlines() if line.startswith('fix '))
        self.assertEqual(['110', '60', '0', 'reported', 'reported'],
                         [cell.strip() for cell in row.split('|')][5:])

    def test_failed_calls_keep_usage_and_exit_status(self):
        with usage.operation(self.root, legacy=False):
            result = self.call(code=7)
        self.assertEqual(7, result.returncode)
        self.assertEqual(1, self.report()['calls'])
        self.assertEqual('partial', self.report()['roles'][0]['coverage'])
        record = next(iter(read_json(self.root / 'usage/ledger.json')['calls'].values()))
        self.assertEqual(('failed', 7), (record['status'], record['exit_code']))

    def test_cancellation_before_launch_never_reads_quota_or_records_call(self):
        with usage.operation(self.root, legacy=False):
            with cancellation_scope() as cancel:
                cancel.request()
                with self.assertRaises(Cancelled):
                    self.call()
                cancel.signum = 0
        self.probe.assert_not_called()
        self.assertEqual(0, self.report()['calls'])

    def test_failed_spawn_is_not_a_charged_call(self):
        with usage.operation(self.root, legacy=False):
            with self.assertRaises(ApiError):
                run(['/no/such/model'], model_events=True, usage_call=usage.choice('fix', {'provider':'codex', 'model':'sol', 'effort':'high'}))
        self.assertEqual(0, self.report()['calls'])

    def test_crashed_invocations_are_recovered_once_without_reexecution(self):
        with usage.operation(self.root, legacy=False):
            self.call()
        file = self.root / 'usage/ledger.json'
        data = read_json(file)
        call = next(iter(data['calls'].values()))
        call['status'] = 'running'
        data['invocations'][call['invocation']].update(status='running', pid=999999999, process_start='gone')
        write_json(file, data)
        self.probe.reset_mock()
        for _ in range(2):
            with usage.operation(self.root):
                pass
        self.assertEqual(1, self.report()['calls'])
        self.probe.assert_not_called()
        self.assertEqual('interrupted', next(iter(read_json(file)['calls'].values()))['status'])

    def test_readings_before_and_after_each_call_are_role_specific(self):
        self.probe.side_effect = [reading(10), reading(12), reading(14), reading(17)]
        with usage.operation(self.root, legacy=False):
            self.call(role='fix')
            self.call(role='synthesis')
        value = self.report()
        self.assertEqual([2, 3], [r['quota'][0]['consumed_points'] for r in value['roles']])
        self.assertEqual(7, value['quota'][0]['consumed_points'])  # Two points between calls remain unattributed.

    def test_scoped_meters_remain_separate_from_overall_meter(self):
        self.probe.side_effect = [reading(10, scoped=20), reading(12, scoped=27)]
        with usage.operation(self.root, legacy=False):
            self.call(claude(), provider='claude')
        self.assertEqual([2, 7], [m['consumed_points'] for m in self.report()['quota']])
        self.assertEqual([2, 7], [m['consumed_points'] for m in self.report()['roles'][0]['quota']])

    def test_reset_timestamp_rounding_does_not_create_false_weekly_resets(self):
        self.probe.side_effect = [reading(10, reset=100000.1), reading(12, reset=100000.9)]
        with usage.operation(self.root, legacy=False):
            self.call()
        self.assertEqual(2, self.report()['quota'][0]['consumed_points'])

    def test_weekly_reset_inside_call_is_unknown_not_negative_consumption(self):
        self.probe.side_effect = [reading(99), reading(1, reset=200000), reading(1, reset=200000), reading(3, reset=200000)]
        with usage.operation(self.root, legacy=False):
            self.call()
            self.call()
        self.assertEqual(2, self.report()['quota'][0]['consumed_points'])
        self.assertEqual('partial', self.report()['quota'][0]['coverage'])

    def test_multiple_weeks_can_exceed_one_hundred_percentage_points(self):
        self.probe.side_effect = [reading(0), reading(99), reading(0, reset=200000), reading(5, reset=200000)]
        with usage.operation(self.root, legacy=False):
            self.call()
            self.call()
        self.assertEqual(104, self.report()['quota'][0]['consumed_points'])

    def test_decreasing_readings_do_not_double_count_a_recovery(self):
        self.probe.side_effect = [reading(10), reading(12), reading(11), reading(13)]
        with usage.operation(self.root, legacy=False):
            self.call()
            self.call()
        self.assertEqual(3, self.report()['quota'][0]['consumed_points'])
        self.assertEqual('partial', self.report()['quota'][0]['coverage'])

    def test_unavailable_quota_is_unknown_while_tokens_continue(self):
        self.probe.return_value = {'status': 'unavailable', 'meters': []}
        with usage.operation(self.root, legacy=False):
            self.call()
        value = self.report()
        self.assertEqual('reported', value['roles'][0]['token_coverage'])
        self.assertIsNone(value['quota'][0]['consumed_points'])
        self.assertEqual('unavailable', value['quota'][0]['coverage'])

    def test_telemetry_and_storage_failures_do_not_change_model_outcome(self):
        self.probe.side_effect = ValueError('unavailable')
        with usage.operation(self.root, legacy=False):
            self.assertEqual(0, self.call().returncode)
        file = self.root / 'usage/ledger.json'
        file.write_text('{bad json')
        with usage.operation(self.root):
            self.assertEqual(0, self.call().returncode)
        self.assertEqual('{bad json', file.read_text())
        self.assertEqual('unavailable', self.report()['coverage'])

    def test_error_results_expose_usage_and_still_raise_original_error(self):
        with self.assertRaises(ApiError) as caught:
            with usage.operation(self.root, legacy=False):
                self.call()
                raise ApiError('original', 'Original workflow stop')
        self.assertEqual('original', caught.exception.code)
        self.assertEqual(1, caught.exception.details['usage']['calls'])
        self.assertTrue(any('Provider / meter' in line for line in self.messages))

    def test_planning_is_measured_before_any_approved_execution_scope(self):
        from pr_review.issue_planning import assess_issue
        value = {'decision': 'no_change', 'summary': 'Done', 'questions': [], 'acceptance': [], 'plan': '', 'inspected': [], 'decisions': []}
        events = claude()
        events[-1]['structured_output'] = value
        argv = [sys.executable, '-c', 'print(' + repr('\n'.join(json.dumps(e) for e in events)) + ')']
        with usage.operation(self.root, legacy=False), \
                patch('pr_review.issue_planning.argv', return_value=argv), \
                patch('pr_review.issue_planning.configuration_snapshot', return_value={}), \
                patch('pr_review.issue_planning.specification.check_assessment'):
            assess_issue({}, {'planning': {'provider': 'claude', 'model': 'fable', 'effort': 'medium'}}, self.root / 'assessments', self.root)
        self.assertEqual('planning', self.report()['roles'][0]['role'])
        self.assertEqual('medium', self.report()['roles'][0]['effort'])

    def test_completed_invocations_stay_completed_on_resume(self):
        for _ in range(2):
            with usage.operation(self.root, legacy=False):
                self.call()
        data = read_json(self.root / 'usage/ledger.json')
        self.assertEqual(['completed', 'completed'], [v['status'] for v in data['invocations'].values()])

    def test_cumulative_claude_checkpoint_survives_crash_before_process_finalization(self):
        with self.assertRaises(ApiError):
            with usage.operation(self.root, legacy=False):
                call = usage.begin_call(usage.choice('fix', {'provider':'claude', 'model':'fable', 'effort':'high'}), {})
                call.launched()
                for event in claude(100):
                    call.event(event)
                raise ApiError('crash', 'Crash after observing durable completion')
        with usage.operation(self.root):
            self.call(claude(120), provider='claude', resumed=True)
        role = self.report()['roles'][0]
        self.assertEqual(207, role['usage']['total_tokens'])
        self.assertEqual('partial', role['token_coverage'])

    def test_unknown_launch_after_crash_is_visible_without_inventing_a_call(self):
        with self.assertRaises(ApiError):
            with usage.operation(self.root, legacy=False):
                usage.begin_call(usage.choice('fix', {'provider':'codex', 'model':'sol', 'effort':'high'}), {})
                raise ApiError('crash', 'Crash around launch boundary')
        with usage.operation(self.root):
            pass
        self.assertEqual(0, self.report()['calls'])
        self.assertEqual(1, self.report()['calls_launch_unknown'])

    def test_role_and_job_totals_expose_unattributed_changes(self):
        self.probe.side_effect = [reading(10), reading(12), reading(14), reading(17)]
        with usage.operation(self.root, legacy=False):
            self.call(role='fix')
            self.call(role='synthesis')
        self.assertEqual(2, self.report()['quota'][0]['unattributed_points'])

    def test_older_claude_per_call_model_totals_are_not_subtracted_as_cumulative(self):
        for count in (100, 140):
            events = claude(count)
            events[0]['claude_code_version'] = '2.1.200'
            with usage.operation(self.root, legacy=False):
                self.call(events, provider='claude', resumed=count != 100)
        self.assertEqual(414, self.report()['roles'][0]['usage']['total_tokens'])

    def test_resource_limit_expiring_during_pre_read_prevents_model_launch(self):
        metadata = usage.choice('fix', {'provider':'codex', 'model':'sol', 'effort':'high'})
        with usage.operation(self.root, legacy=False), \
                patch('pr_review.issue_resources.check', side_effect=[None, ApiError('resource_limit', 'expired')]), \
                patch('pr_review.processes.subprocess.Popen') as launch:
            with self.assertRaises(ApiError):
                run(['never-launched'], model_events=True, usage_call=metadata)
            launch.assert_not_called()
        self.assertEqual(0, self.report()['calls'])

    def test_session_reads_add_usage_without_changing_draft_revision(self):
        import test_api
        from pr_review.application import dispatch
        fixture = test_api.ApiTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        request = {'command': 'read', 'session': str(fixture.path)}
        before = dispatch(request)
        with usage.operation(fixture.path, legacy=True):
            self.call()
        after = dispatch(request)
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(before['generation'], after['generation'])
        self.assertEqual(1, after['usage']['calls'])
        self.assertTrue(after['usage']['historical_usage_unknown'])

    def test_active_usage_tables_are_retained_in_nested_operation_logs(self):
        from pr_review.operations import operation
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root.parent / 'state')}):
            with usage.operation(self.root, legacy=False):
                with operation({'kind': 'test'} ) as op:
                    self.call()
            text = Path(op['log_path']).read_text()
        self.assertIn('Provider / meter', text)
        self.assertIn('Reported tokens', text)
        self.assertIn('sol', text)

    def test_unexpected_io_error_preserves_usage_in_api_error(self):
        from pr_review import api
        def fail(request):
            with usage.operation(self.root, legacy=False):
                self.call()
                raise OSError('original IO failure')
        output = StringIO()
        with patch('pr_review.application.dispatch', side_effect=fail), \
                patch('sys.stdin', StringIO('{"protocol":1,"command":"run"}\n')), \
                redirect_stdout(output), self.assertRaises(SystemExit):
            api.main()
        error = json.loads(output.getvalue().splitlines()[-1])['error']
        self.assertEqual('original IO failure', error['message'])
        self.assertEqual(1, error['usage']['calls'])

    def test_keyboard_interrupt_preserves_usage_for_cli_json_adapter(self):
        with self.assertRaises(KeyboardInterrupt) as caught:
            with usage.operation(self.root, legacy=False):
                self.call()
                raise KeyboardInterrupt()
        self.assertEqual(1, caught.exception.usage['calls'])


class TokenNormalizationTest(unittest.TestCase):
    def test_cache_and_reasoning_are_not_counted_twice(self):
        self.assertEqual(110, usage.tokens(codex()[-1]['usage'])['total_tokens'])
        self.assertEqual(3, usage.tokens(codex()[-1]['usage'])['reasoning_output_tokens'])
        value = usage.tokens({'input_tokens': 10, 'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 5, 'output_tokens': 4}, provider='claude')
        self.assertEqual((65, 69), (value['input_tokens'], value['total_tokens']))

    def test_unknown_or_invalid_counters_are_not_silently_zero(self):
        for value in (None, {}, {'input_tokens': True, 'output_tokens': 0}, {'input_tokens': -1, 'output_tokens': 0}):
            self.assertIsNone(usage.tokens(value))


class QuotaReaderTest(unittest.TestCase):
    def test_codex_selects_weekly_windows_by_duration_and_keeps_pools(self):
        result = {'id': 2, 'result': {'rateLimitsByLimitId': {
            'a': {'primary': {'windowDurationMins': 10080, 'usedPercent': 12, 'resetsAt': 100000},
                  'secondary': {'windowDurationMins': 300, 'usedPercent': 90, 'resetsAt': 100000}},
            'b': {'secondary': {'windowDurationMins': 10080, 'usedPercent': 22, 'resetsAt': 200000}}}}}
        with patch('pr_review.usage_quota.shutil.which', return_value='/bin/provider'), patch('pr_review.usage_quota._records', return_value=[result]):
            value = usage_quota.read('codex', {})
        self.assertEqual(['a:primary', 'b:secondary'], [m['id'] for m in value['meters']])
        self.assertEqual([12, 22], [m['used'] for m in value['meters']])

    def test_claude_uses_structured_report_and_ignores_text(self):
        row = {'kind': 'weekly_scoped', 'percent': 33, 'resets_at': '2026-11-05T10:00:00Z',
               'scope': {'model': {'display_name': 'Fable'}}}
        records = [{'type': 'assistant', 'usage_report': {'rate_limits': {'limits': [row]}}},
                   {'type': 'result', 'total_cost_usd': 0, 'is_error': False}]
        with patch('pr_review.usage_quota.shutil.which', return_value='/bin/provider'), patch('pr_review.usage_quota._records', return_value=records) as transport:
            value = usage_quota.read('claude', {})
            args = transport.call_args.args[0]
        self.assertIn('/usage', args)
        self.assertEqual('', args[args.index('--tools') + 1])
        self.assertEqual('Anthropic weekly, Fable', value['meters'][0]['label'])
        row['scope'] = None
        with patch('pr_review.usage_quota.shutil.which', return_value='/bin/provider'), patch('pr_review.usage_quota._records', return_value=records):
            self.assertEqual('Anthropic weekly, unspecified scope', usage_quota.read('claude', {})['meters'][0]['label'])
        records[0] = {'type': 'assistant', 'message': {'content': 'Weekly usage: 33%'}}
        with patch('pr_review.usage_quota.shutil.which', return_value='/bin/provider'), patch('pr_review.usage_quota._records', return_value=records):
            self.assertEqual('unavailable', usage_quota.read('claude', {})['status'])

    def test_api_credentials_cannot_be_assigned_subscription_usage(self):
        for provider, key in (('codex', 'OPENAI_API_KEY'), ('claude', 'ANTHROPIC_API_KEY')):
            with patch('pr_review.usage_quota._records') as transport:
                self.assertEqual('unavailable', usage_quota.read(provider, {key: 'not-printed'})['status'])
                transport.assert_not_called()

    def test_transport_is_bounded_for_silent_or_oversized_metadata_commands(self):
        for script in ('import time; time.sleep(10)', 'print("x" * (3 * 1024 * 1024))'):
            self.assertEqual([], usage_quota._records([sys.executable, '-c', script], dict(os.environ), .1))
