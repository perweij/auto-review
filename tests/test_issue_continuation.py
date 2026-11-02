"""Autonomous issue continuation, interruption, and user-facing stops."""
from pathlib import Path
import unittest
from unittest.mock import patch

import engine_stub
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

    def engines(self, fix=None, review=None):
        """Patch the implementation turn and the engine review separately."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch('pr_review.issue_harness.invoke', side_effect=fix or self.t.harness))
        stack.enter_context(patch('pr_review.acceptance.review_issue',
                                  side_effect=engine_stub.issue_review_from(review or self.t.review)))
        return stack

    def test_review_findings_return_to_implementation_until_verified(self):
        reviews = 0
        calls = []
        def fix(session, state, batch, specification, feedback):
            calls.append(('fix', feedback))
            return self.t.harness(session, state, batch, specification, feedback)
        def review(session, state, batch, specification):
            nonlocal reviews
            calls.append(('review', None))
            report = self.t.review(session, state, batch, specification)
            reviews += 1
            if reviews <= 2:
                report.update(status='changes_requested', findings=['Round-trip regression'])
            return report
        with self.engines(fix, review):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(['fix', 'review'] * 3, [r for r, _ in calls])
        self.assertEqual(['Round-trip regression'], calls[2][1])
        batch = self.owner.load()['batches'][-1]
        self.assertTrue(batch['commit_progress']['complete'])

    def test_unchanged_rejected_candidate_stops_without_publishing_success(self):
        def review(*args):
            report = self.t.review(*args)
            report.update(status='changes_requested', findings=['Concrete defect'])
            return report
        with self.engines(review=review):
            result = self.run_issue()
        self.assertEqual('automation_failed', result['outcome'])
        self.assertTrue(self.t.f['pr']['draft'])
        self.assertFalse(self.owner.load()['batches'][-1].get('commit_execution'))
        notices = [c['body'] for c in self.t.f['comments'] if 'fix-issue stopped:' in c['body']]
        self.assertEqual(1, len(notices))
        self.assertNotIn('CLI', notices[0])

    def test_interrupted_recorded_source_resumes_without_manual_repair(self):
        def interrupted(*args):
            self.t.harness(*args)
            raise ApiError('model_failed', 'Provider interrupted')
        with self.engines(fix=interrupted):
            first = self.run_issue()
        self.assertEqual('model_failed', first['error']['code'])
        self.t.assessment['decision'] = 'continue'
        with self.engines():
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertTrue(self.owner.load()['batches'][-1]['commit_progress']['complete'])

    def test_internal_work_returns_to_harness_without_intermediate_review(self):
        calls = []
        def fix(*args):
            calls.append('fix')
            report = self.t.harness(*args)
            if len(calls) == 1:
                report.update(status='working', findings=['Finish internal representation'])
            return report
        def review(*args):
            calls.append('review')
            return self.t.review(*args)
        with self.engines(fix, review):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(['fix', 'fix', 'review'], calls)
        self.assertFalse(any('clarification' in c['body'] for c in self.t.f['comments']))

    def test_product_question_is_published_without_futile_repair_loop(self):
        question = 'Should archived records remain searchable, or be hidden?'
        def harness(*args):
            return {'status': 'needs_input', 'summary': 'Unspecified archive behavior',
                    'questions': [question], 'findings': [], 'checks': []}
        with patch('pr_review.issue_harness.invoke', side_effect=harness) as invoke, \
                patch('pr_review.acceptance.review_issue', side_effect=AssertionError('No review for a question')):
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

    def engine_stop(self, code, error, guidance):
        from pr_review import review_engine
        incomplete = review_engine.Result({'assessment': 'incomplete', 'summary': 'The environment could not run the tests'},
                                          'incomplete', Path('/retained/synthesis-incomplete-1.json'))
        with patch('pr_review.issue_harness.invoke', side_effect=self.t.harness), \
                patch('pr_review.acceptance.review_issue', side_effect=error or (lambda *args: (incomplete, None))):
            result = self.run_issue()
        self.assertEqual(code, result['error']['code'], result)
        notices = [c['body'] for c in self.t.f['comments'] if 'fix-issue stopped:' in c['body']]
        self.assertEqual(1, len(notices), notices)
        self.assertIn(guidance, notices[0])
        self.assertEqual('review', self.owner.load()['batches'][-1]['harness_phase'])
        self.assertTrue(self.t.f['pr']['draft'])

    def test_an_incomplete_review_stop_is_visible_and_resumable(self):
        self.engine_stop('review_incomplete', None, 'rerun only the stages without valid evidence')

    def test_an_independence_failure_stop_is_visible_and_never_falls_back(self):
        self.engine_stop('independence_failed', ApiError('independence_failed', 'workspace preparation failed'), 'never runs without its workspace')

    def test_a_modified_checkout_stop_says_no_adoption_is_needed(self):
        self.engine_stop('review_checkout_modified', ApiError('review_checkout_modified', 'checkout changed'), 'no adoption is needed')
