"""Explicit CLI replanning delegates to Owner without bypassing recovery guards."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_issues
import test_issue_steps
from pr_review.common import ApiError
from pr_review.issues import dispatch
from pr_review import owner_state


class ReplanCliTest(unittest.TestCase):
    def test_cli_forwards_replan_for_issue_and_pr(self):
        helper = test_issues.WriteRecoveryCliTest()
        for target in ('-i', '-p'):
            code, _, call = helper.invoke([target, '12', '--replan', '--json'],
                                         return_value={'outcome': 'needs_attention', 'exit_code': 3})
            self.assertEqual(3, code)
            self.assertTrue(call.call_args.args[0]['replan'])
            self.assertEqual('issue-run', call.call_args.args[0]['command'])

    def test_replan_cannot_be_combined_with_write_recovery(self):
        helper = test_issues.WriteRecoveryCliTest()
        for option in (['--inspect-writes'], ['--retry-absent', 'plan:1']):
            with patch('pr_review.issues.resolve') as resolve:
                code, output, _ = helper.invoke(['-i', '12', '--replan', *option, '--json'],
                                                side_effect=dispatch)
            self.assertNotEqual(0, code)
            self.assertIn('not write inspection or recovery', output)
            resolve.assert_not_called()

    def test_replan_api_requires_boolean_before_io(self):
        with patch('pr_review.issues.resolve') as resolve, self.assertRaisesRegex(ApiError, 'Boolean'):
            dispatch({'command': 'issue-run', 'issue': 12, 'replan': 'yes'})
        resolve.assert_not_called()


class ReplanIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_no_existing_batch_is_rejected_before_models_or_publication(self):
        before = copy.deepcopy(self.t.f['comments'])
        result = dispatch({**self.t.req, 'replan': True})
        self.assertEqual('invalid_request', result['error']['code'])
        self.assertEqual([], self.t.assessments)
        self.assertEqual([], self.t.calls)
        self.assertEqual(before, self.t.f['comments'])

    def test_completed_batch_is_not_reopened(self):
        self.assertEqual(0, dispatch({**self.t.req, 'yolo': True})['exit_code'])
        before = copy.deepcopy(self.t.f['comments'])
        calls = len(self.t.calls)
        result = dispatch({**self.t.req, 'replan': True})
        self.assertEqual('invalid_request', result['error']['code'])
        self.assertEqual(calls, len(self.t.calls))
        self.assertEqual(before, self.t.f['comments'])

    def test_exhausted_task_requires_explicit_replan_and_retains_history(self):
        original = self.t.model
        def rejected(*args):
            report = original(*args)
            if args[3] == 'review':
                report['blocking'] = ['Concrete defect remains']
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=rejected):
            first = dispatch({**self.t.req, 'yolo': True})
            self.assertEqual(3, first['exit_code'])
            self.assertEqual('await_recovery_authorization', first['next_action'])
            self.assertEqual(first['next_action'], first['eligibility']['next_action'])
            notices = [c for c in self.t.f['comments'] if '## fix-issue paused:' in c['body']]
            self.assertEqual(1, len(notices))
            self.assertIn('Concrete defect remains', notices[0]['body'])
            self.assertIn('/fix-issue retry ', notices[0]['body'])
            self.assertNotIn('CLI', notices[0]['body'])
            self.assertNotIn(str(self.t.path), notices[0]['body'])
            owner = owner_state.OwnerSession({'session': self.t.path})
            old = copy.deepcopy(owner.load()['batches'][-1])
            self.assertEqual(3, old['tasks'][0]['repairs'])
            # Initial bootstrap adds PR context. Keep requirements unchanged
            # when that context is reassessed, rather than requesting a new plan.
            self.t.assessment['decision'] = 'continue'
            calls = len(self.t.calls)
            again = dispatch({**self.t.req, 'yolo': True})
            self.assertEqual(3, again['exit_code'])
            self.assertEqual(calls, len(self.t.calls))
            self.assertEqual(notices, [c for c in self.t.f['comments'] if '## fix-issue paused:' in c['body']])
        waiting = dispatch({**self.t.req, 'replan': True})
        self.assertEqual(3, waiting['exit_code'])
        self.assertEqual('stale_approval', waiting['error']['code'])
        self.assertEqual(1, len(owner.load()['batches'][-1]['plans']))
        result = dispatch({**self.t.req, 'replan': True, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        batch = owner.load()['batches'][-1]
        self.assertEqual(old['id'], batch['id'])
        self.assertEqual(2, len(batch['plans']))
        self.assertEqual(old['tasks'], batch['plans'][-1]['previous_tasks'])
        self.assertEqual(3, batch['plans'][-1]['previous_tasks'][0]['repairs'])
        self.assertEqual(0, batch['tasks'][0]['repairs'])
        self.assertEqual('complete', batch['tasks'][0]['status'])
        self.assertEqual(2, len(owner.load()['issue_recovery']['attempts']))
        self.assertEqual(2, owner.load()['issue_recovery']['limit'])


class ReplanStepTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issue_steps.IssueStepsTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_replan_preserves_completed_step_and_is_consumed_once(self):
        self.t.fail = 'pipeline'
        batch = self.t.start()['batches'][-1]
        completed = copy.deepcopy(batch['tasks'][0])
        self.t.fail = None
        flow = self.t.flow()
        flow.request['replan'] = True
        result = flow.fix(self.t.owner, [self.t.key])
        self.assertEqual(completed['id'], result['tasks'][0]['id'])
        self.assertEqual(completed['after'], result['tasks'][0]['after'])
        self.assertEqual(completed['review'], result['tasks'][0]['review'])
        self.assertEqual(2, len(result['plans']))
        # A subsequent corrective batch in this invocation must not re-use the flag.
        with patch('pr_review.owner_fixes.execute', side_effect=ApiError('test_stop', 'Request captured')) as execute:
            with self.assertRaisesRegex(ApiError, 'Request captured'):
                flow.fix(self.t.owner, [self.t.key])
        self.assertNotIn('replan', execute.call_args.args[2])
        self.assertTrue(flow._replan_used)

    def test_replan_does_not_adopt_manual_workspace_changes(self):
        self.t.fail = 'pipeline'
        view = self.t.start()
        path = Path(view['worktree']) / 'manual.txt'
        path.write_text('keep this edit\n')
        flow = self.t.flow()
        flow.request['replan'] = True
        self.t.calls.clear()
        with self.assertRaisesRegex(ApiError, 'adopt'):
            flow.fix(self.t.owner, [self.t.key])
        self.assertEqual('keep this edit\n', path.read_text())
        self.assertEqual([], self.t.calls)

    def test_replan_does_not_bypass_frozen_settings(self):
        self.t.fail = 'pipeline'
        self.t.start()
        data = self.t.owner.load()
        data['settings']['fix']['model'] = 'changed-model'
        self.t.owner.save(data)
        flow = self.t.flow()
        flow.request['replan'] = True
        self.t.calls.clear()
        with self.assertRaisesRegex(ApiError, 'saved provider settings'):
            flow.fix(self.t.owner, [self.t.key])
        self.assertEqual([], self.t.calls)

    def test_all_tasks_complete_requires_commit_recovery_not_replan(self):
        self.t.start()
        flow = self.t.flow()
        flow.state['owner'] = str(self.t.owner.path)
        flow.request['replan'] = True
        self.t.calls.clear()
        with self.assertRaisesRegex(ApiError, 'commit recovery separately'):
            flow.run()
        self.assertEqual([], self.t.calls)
        flow.writes.reconcile.assert_not_called()

    def test_changed_replan_target_cannot_replan_another_batch(self):
        self.t.fail = 'pipeline'
        self.t.start()
        flow = self.t.flow()
        flow.request['replan'] = True
        flow._replan_batch = 'different-batch'
        self.t.calls.clear()
        with self.assertRaisesRegex(ApiError, 'selected for replanning changed'):
            flow.fix(self.t.owner, [self.t.key])
        self.assertEqual([], self.t.calls)
