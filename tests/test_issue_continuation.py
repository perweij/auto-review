"""Autonomous issue continuation, interruption, and user-facing stops."""
import unittest
from unittest.mock import patch

import test_issues
from pr_review import owner_state
from pr_review.common import ApiError
from pr_review.issues import dispatch


class ContinuationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.owner = owner_state.OwnerSession({'session': self.t.path})

    def run_issue(self):
        return dispatch({**self.t.req, 'yolo': True})

    def test_review_findings_return_to_implementation_until_verified(self):
        original = self.t.harness
        reviews = 0
        calls = []
        def harness(session, state, batch, role, specification, feedback):
            nonlocal reviews
            calls.append((role, feedback))
            report = original(session, state, batch, role, specification, feedback)
            if role == 'review':
                reviews += 1
                if reviews <= 2:
                    report.update(status='changes_requested', findings=['Round-trip regression'])
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=harness):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(['fix', 'review'] * 3, [r for r, _ in calls])
        self.assertEqual(['Round-trip regression'], calls[2][1])
        batch = self.owner.load()['batches'][-1]
        self.assertTrue(batch['commit_progress']['complete'])

    def test_unchanged_rejected_candidate_stops_without_publishing_success(self):
        original = self.t.harness
        def harness(*args):
            report = original(*args)
            if args[3] == 'review':
                report.update(status='changes_requested', findings=['Concrete defect'])
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=harness):
            result = self.run_issue()
        self.assertEqual('automation_failed', result['outcome'])
        self.assertTrue(self.t.f['pr']['draft'])
        self.assertFalse(self.owner.load()['batches'][-1].get('commit_execution'))
        notices = [c['body'] for c in self.t.f['comments'] if 'fix-issue stopped:' in c['body']]
        self.assertEqual(1, len(notices))
        self.assertNotIn('CLI', notices[0])

    def test_interrupted_recorded_source_resumes_without_manual_repair(self):
        original = self.t.harness
        def interrupted(*args):
            original(*args)
            raise ApiError('model_failed', 'Provider interrupted')
        with patch('pr_review.issue_harness.invoke', side_effect=interrupted):
            first = self.run_issue()
        self.assertEqual('model_failed', first['error']['code'])
        self.t.assessment['decision'] = 'continue'
        result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertTrue(self.owner.load()['batches'][-1]['commit_progress']['complete'])

    def test_internal_work_returns_to_harness_without_intermediate_review(self):
        original = self.t.harness
        calls = []
        def harness(*args):
            calls.append(args[3])
            report = original(*args)
            if len(calls) == 1:
                report.update(status='working', findings=['Finish internal representation'])
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=harness):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(['fix', 'fix', 'review'], calls)
        self.assertFalse(any('clarification' in c['body'] for c in self.t.f['comments']))

    def test_product_question_is_published_without_futile_repair_loop(self):
        question = 'Should archived records remain searchable, or be hidden?'
        def harness(*args):
            return {'status': 'needs_input', 'summary': 'Unspecified archive behavior',
                    'questions': [question], 'findings': [], 'checks': []}
        with patch('pr_review.issue_harness.invoke', side_effect=harness) as invoke:
            result = self.run_issue()
        self.assertEqual('await_answers', result['next_action'], result)
        self.assertEqual(1, invoke.call_count)
        questions = [c['body'] for c in self.t.f['comments'] if 'Implementation needs clarification:' in c['body']]
        self.assertEqual(1, len(questions))
        self.assertIn(question, questions[0])
        self.assertTrue(self.t.f['pr']['draft'])

    def test_configuration_trust_stop_is_visible_on_github(self):
        with patch('pr_review.issue_harness.invoke', side_effect=ApiError('trust_required', 'Inspect local agent config')):
            result = self.run_issue()
        self.assertEqual('trust_required', result['error']['code'])
        notices = [c['body'] for c in self.t.f['comments'] if 'fix-issue stopped:' in c['body']]
        self.assertEqual(1, len(notices))
        self.assertIn('requires explicit trust', notices[0])
        self.assertNotIn('Inspect local agent config', notices[0])
