"""Issue recovery is bounded across resumes, with authorization through GitHub."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_issues
from pr_review import owner_fixes, owner_recovery, owner_state
from pr_review.common import ApiError, atomic_json, digest
from pr_review.issues import dispatch, semantic_comments


class RecoveryBudgetTest(unittest.TestCase):
    def test_question_publication_requires_current_independent_review(self):
        question = {'status': 'clarify', 'evidence': 'Choose behavior A or B?'}
        implementation = {'outcomes': [question]}
        task = {'status': 'needs_attention', 'implementation': implementation,
                'after': {'tree': 'current'}, 'review': {'outcomes': [question]}}
        batch = {'tasks': [task], 'checkpoint': {'tree': 'current'},
                 'review': {'blocking': ['Technical defect, not a question']}}
        self.assertEqual([], owner_fixes.clarification_questions(batch))
        task['assessment_review_report'] = digest(implementation)
        self.assertEqual(['Choose behavior A or B?'], owner_fixes.clarification_questions(batch))
        task['after']['tree'] = 'old'
        self.assertEqual([], owner_fixes.clarification_questions(batch))

    def test_reservations_survive_batches_and_authorizations_are_single_use(self):
        state = {'issue_workflow': 'workflow'}
        batch = {'id': 'batch', 'checkpoint': {'tree': 'tree'}, 'automatic_recovery': True}
        task = {'id': 'task'}
        for _ in range(2):
            self.assertIsNotNone(owner_recovery.reserve(state, batch, task, {'blocking': ['bug']}))
        self.assertIsNone(owner_recovery.reserve(state, {**batch, 'id': 'new'}, task, {}))
        request = copy.deepcopy(task['recovery_request'])
        comment = {'id': 1, 'user': {'login': 'Author'}, 'created_at': request['created_at'],
                   'body': '/fix-issue retry ' + request['token']}
        for change in ({'user': {'login': 'other'}}, {'body': '> ' + comment['body']},
                       {'created_at': '2000-01-01'}, {'body': comment['body'] + '\nAlso change behavior'}):
            self.assertFalse(owner_recovery.authorize(state, [{**comment, **change}], 'author'))
        self.assertTrue(owner_recovery.authorize(state, [comment], 'author'))
        self.assertFalse(owner_recovery.authorize(state, [comment], 'author'))
        self.assertEqual(4, state['issue_recovery']['limit'])
        for _ in range(2):
            self.assertIsNotNone(owner_recovery.reserve(state, batch, task, {}))
        self.assertIsNone(owner_recovery.reserve(state, batch, task, {}))
        self.assertNotEqual(request['token'], task['recovery_request']['token'])
        self.assertFalse(owner_recovery.authorize(state, [comment], 'author'))

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
        self.reviews = 0
        self.fail_until = 100
        self.recoveries = []
        self.crash = False
        original = self.t.model

        def model(session, state, batch, role, context):
            if role == 'fix' and context.get('focused_recovery'):
                self.recoveries.append(copy.deepcopy(context))
                if self.crash:
                    raise ApiError('provider_failed', 'Interrupted recovery')
            report = original(session, state, batch, role, context)
            if role == 'review' and not context.get('scope'):
                self.reviews += 1
                if self.reviews <= self.fail_until:
                    report.update(assessment='changes_requested', blocking=['Round-trip regression'])
            return report
        provider = patch('pr_review.owner_providers.invoke', side_effect=model)
        provider.start()
        self.addCleanup(provider.stop)

    def run_issue(self):
        return dispatch({**self.t.req, 'yolo': True})

    def test_saved_rejection_recovers_without_replan_and_final_review_precedes_commit(self):
        # Simulate an old one-repair paused run without the new recovery policy.
        with patch('pr_review.owner_recovery.reserve', return_value=None):
            stopped = self.run_issue()
        self.assertEqual(3, stopped['exit_code'])
        before = self.owner.load()['batches'][-1]
        self.assertEqual(1, before['tasks'][0]['repairs'])
        self.t.assessment['decision'] = 'continue'
        self.fail_until = self.reviews
        result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        state = self.owner.load()
        batch = state['batches'][-1]
        self.assertEqual(before['id'], batch['id'])
        self.assertEqual(len(before['plans']), len(batch['plans']))
        self.assertEqual(1, len(self.recoveries))
        self.assertEqual(['Round-trip regression'], self.recoveries[0]['review_findings']['blocking'])
        self.assertEqual('passed', state['issue_recovery']['attempts'][0]['status'])
        self.assertTrue(batch['review']['complete'])
        self.assertTrue(batch['commit_progress']['complete'])

    def test_exhaustion_resume_and_github_authorization_without_replan(self):
        first = self.run_issue()
        self.assertEqual('await_recovery_authorization', first['next_action'])
        state = self.owner.load()
        self.assertEqual(2, len(state['issue_recovery']['attempts']))
        batch = copy.deepcopy(state['batches'][-1])
        self.assertEqual(batch['baseline'], self.t.git('rev-parse', 'HEAD', cwd=state['worktree']))
        calls = len(self.t.calls)
        self.t.assessment['decision'] = 'continue'
        again = self.run_issue()
        self.assertEqual(first['next_action'], again['next_action'])
        self.assertEqual(calls, len(self.t.calls))
        self.assertEqual(1, len([c for c in self.t.f['comments'] if 'automatic recovery allowance' in c['body']]))
        request = state['issue_recovery']['request']
        self.t.f['comments'].append({'id': 10000, 'user': {'login': 'author'},
            'body': '/fix-issue retry ' + request['token'], 'created_at': request['created_at']})
        self.fail_until = self.reviews
        result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        state = self.owner.load()
        self.assertEqual(3, len(state['issue_recovery']['attempts']))
        self.assertEqual(1, len(state['issue_recovery']['authorizations']))
        self.assertEqual(len(batch['plans']), len(state['batches'][-1]['plans']))

    def test_interruption_consumes_credit_and_does_not_adopt_changes(self):
        self.crash = True
        first = self.run_issue()
        self.assertEqual(3, first['exit_code'])
        self.assertEqual(1, len(self.owner.load()['issue_recovery']['attempts']))
        calls = len(self.t.calls)
        again = self.run_issue()
        self.assertEqual('interrupted_task', again['error']['code'])
        self.assertEqual(calls, len(self.t.calls))
        self.assertEqual(1, len(self.recoveries))

    def test_incomplete_verification_does_not_launch_repair(self):
        original = self.t.model
        def incomplete(*args):
            report = original(*args)
            if args[3] == 'review':
                report.update(assessment='incomplete', complete=False,
                              blocking=['Test environment is unavailable'])
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=incomplete):
            result = self.run_issue()
        self.assertEqual('retry', result['next_action'])
        self.assertNotIn('issue_recovery', self.owner.load())
        self.assertEqual(1, len([c for role, c in self.t.calls if role == 'fix']))

    def test_internal_ambiguity_is_reviewed_and_repaired_without_user_question(self):
        original = self.t.model
        fixes = 0
        def model(session, state, batch, role, context):
            nonlocal fixes
            report = original(session, state, batch, role, context)
            if role == 'fix':
                fixes += 1
                if fixes == 1:
                    report.update(complete=False, blocking=['Cannot distinguish normalized state from explicit input'])
                    report['outcomes'][0].update(status='clarify', evidence='Which internal representation should I use?')
            elif role == 'review' and not context.get('scope') and fixes == 1:
                self.assertFalse(context['implementation_report']['complete'])
                report.update(assessment='changes_requested', blocking=['Separate canonical state from override input'])
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(2, fixes)
        self.assertFalse(any('fix-issue paused' in c['body'] for c in self.t.f['comments']))

    def test_saved_incomplete_implementation_retries_assessment_without_blind_refix(self):
        original = self.t.model
        def model(session, state, batch, role, context):
            report = original(session, state, batch, role, context)
            if role == 'fix':
                report.update(complete=False, blocking=['Internal input ambiguity'],
                              case_tree=batch['checkpoint']['tree'])
                report['outcomes'][0].update(status='clarify', evidence='Which internal representation should I use?')
            elif role == 'review':
                report.update(assessment='incomplete', complete=False, blocking=['Verification environment unavailable'])
            return report
        def old_stop(session, state, batch, task):
            task['status'] = 'needs_attention'
            session.save(state)
            return False
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with patch('pr_review.owner_fixes.implementation_ready', side_effect=old_stop):
                initial = self.run_issue()
                self.assertEqual(3, initial['exit_code'])
            # A pre-upgrade run published the fixer's claim and waited on it.
            state_file = Path(initial['session']) / 'session.json'
            saved = json.loads(state_file.read_text())
            saved.update(execution_blockers=['Which internal representation should I use?'],
                         phase='waiting', waiting='answers', reassessment_generation=1)
            atomic_json(state_file, saved)
            self.t.assessment['decision'] = 'continue'
            stopped = self.run_issue()
            self.assertEqual('verification', stopped['error']['stage'])
            saved = json.loads(state_file.read_text())
            self.assertNotIn('execution_blockers', saved)
            self.assertEqual(1, len(saved['retired_execution_blockers']))
            self.assertEqual([], self.t.assessments[-1][0]['execution_blockers'])
            calls = len(self.t.calls)
            self.assertEqual(1, len([c for role, c in self.t.calls if role == 'fix']))
            again = self.run_issue()
            self.assertEqual('verification', again['error']['stage'])
            self.assertEqual(calls + 1, len(self.t.calls))
            self.assertEqual('review', self.t.calls[-1][0])

    def test_independent_pass_preserves_incomplete_fixer_report(self):
        original = self.t.model
        def model(*args):
            report = original(*args)
            if args[3] == 'fix':
                report.update(complete=False, blocking=['Unsure whether the implementation satisfies the contract'])
                report['outcomes'][0].update(status='clarify', evidence='Is my internal representation acceptable?')
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            result = self.run_issue()
        self.assertEqual(0, result['exit_code'], result)
        batch = self.owner.load()['batches'][-1]
        self.assertFalse(batch['tasks'][0]['implementation']['complete'])
        self.assertTrue(batch['tasks'][0]['review']['complete'])
        self.assertTrue(batch['review']['complete'])
        self.assertTrue(batch['commit_progress']['complete'])

    def test_only_review_confirmed_product_question_is_published(self):
        original = self.t.model
        def model(*args):
            report = original(*args)
            if args[3] in ('fix', 'review'):
                report.update(complete=False, blocking=['Missing requirement'])
                if args[3] == 'review':
                    report['assessment'] = 'incomplete'
                report['outcomes'][0].update(status='clarify', evidence=(
                    'Which internal representation should I use?' if args[3] == 'fix'
                    else 'Should archived records remain searchable, or be hidden from search?'))
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            result = self.run_issue()
        self.assertEqual('await_answers', result['next_action'])
        self.assertEqual('verification', result['error']['stage'])
        questions = [c['body'] for c in self.t.f['comments'] if 'Implementation needs clarification:' in c['body']]
        self.assertEqual(1, len(questions))
        self.assertIn('Should archived records remain searchable', questions[0])
        self.assertNotIn('internal representation', questions[0])
        self.assertNotIn('issue_recovery', self.owner.load())
