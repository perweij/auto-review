"""Harness continuation and read-only compatibility with old issue records."""
import unittest
from unittest.mock import patch

import test_issues
from pr_review import issue_migration, owner_state
from pr_review.common import ApiError, digest
from pr_review.issues import dispatch, semantic_comments


class LegacyRecordTest(unittest.TestCase):
    def test_question_publication_requires_current_independent_review(self):
        question = {'status': 'clarify', 'evidence': 'Choose behavior A or B?'}
        implementation = {'outcomes': [question]}
        task = {'status': 'needs_attention', 'implementation': implementation,
                'after': {'tree': 'current'}, 'review': {'outcomes': [question]}}
        batch = {'tasks': [task], 'checkpoint': {'tree': 'current'},
                 'review': {'blocking': ['Technical defect, not a question']}}
        self.assertEqual([], issue_migration.reviewed_questions(batch))
        task['assessment_review_report'] = digest(implementation)
        self.assertEqual(['Choose behavior A or B?'], issue_migration.reviewed_questions(batch))
        task['after']['tree'] = 'old'
        self.assertEqual([], issue_migration.reviewed_questions(batch))


    def test_only_standalone_initiator_control_is_excluded_from_requirements(self):
        state = {'viewer': 'author', 'actions': {}}
        control = {'id': 1, 'user': {'login': 'author'}, 'body': '/fix-issue retry ' + 'a' * 16}
        self.assertEqual([], semantic_comments([control], state))
        for change in ({'user': {'login': 'other'}}, {'body': control['body'] + '\nNew requirement'}):
            self.assertEqual(1, len(semantic_comments([{**control, **change}], state)))


class RecoveryIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.owner = owner_state.OwnerSession({'session': self.t.path})

    def run_issue(self):
        return dispatch({**self.t.req, 'yolo': True})

    def test_review_repairs_continue_without_task_planning_or_retry_tokens(self):
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
        self.assertNotIn('issue_recovery', self.owner.load())
        self.assertFalse(any('/fix-issue retry ' in c['body'] for c in self.t.f['comments']))

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
