"""Interactive Owner issue tasks and issue execution boundaries."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_owner
from pr_review import owner_fixes, owner_import, owner_git, owner_commit_plan
from pr_review.common import ApiError
from pr_review.owner_state import OwnerSession, active_feedback_keys
from pr_review.issues import Workflow


def task(identity, key, depends=()):
    return {'id': identity, 'items': [key], 'depends_on': list(depends),
            'rationale': identity, 'expected': [{'item': key, 'outcome': 'Implement '+identity}]}


class StepPlanTest(unittest.TestCase):
    def test_issue_steps_require_dependency_and_normal_feedback_stays_unique(self):
        plan = {'tasks': [task('config', 'issue'), task('pipeline', 'issue', ['config'])]}
        result = owner_fixes.validate_plan(plan, [{'key': 'issue', 'kind': 'issue_plan'}])
        self.assertEqual(['config', 'pipeline'], [t['id'] for t in result])
        with self.assertRaises(ApiError):
            owner_fixes.validate_plan(plan, [{'key': 'issue', 'kind': 'thread'}])
        plan['tasks'][1]['depends_on'] = []
        with self.assertRaisesRegex(ApiError, 'previous task ID'):
            owner_fixes.validate_plan(plan, [{'key': 'issue', 'kind': 'issue_plan'}])

    def test_duplicate_within_task_and_missing_items_are_rejected(self):
        plan = {'tasks': [task('config', 'issue')]}
        plan['tasks'][0]['items'].append('issue')
        with self.assertRaises(ApiError):
            owner_fixes.validate_plan(plan, [{'key': 'issue', 'kind': 'issue_plan'}])
        with self.assertRaises(ApiError):
            owner_fixes.validate_plan({'tasks': [task('config', 'issue')]},
                                      [{'key': 'issue', 'kind': 'issue_plan'}, {'key': 'other'}])

    def test_skipped_step_does_not_remove_remaining_issue_from_freshness_checks(self):
        batch = {'items': ['issue'], 'tasks': [{**task('a','issue'), 'status':'skipped'},
                                              {**task('b','issue'), 'status':'queued'}]}
        self.assertEqual(['issue'], active_feedback_keys(batch))


class IssueStepsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_owner.OwnerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner = OwnerSession({'session': self.fixture.path})
        self.key = owner_import.from_issue(self.owner,
            {'revision': 1, 'body': 'Implement config then pipeline.', 'acceptance': ['Both work together']},
            {'title': 'Provider slots', 'html_url': 'https://github.com/org/repo/issues/12'}, 'workflow')
        data = self.owner.load()
        data['issue_workflow'] = 'workflow'
        self.owner.save(data)
        self.calls = []
        self.legacy = False
        self.blocked_task = None
        self.final_failure = False
        p = patch('pr_review.owner_providers.invoke', side_effect=self.model)
        p.start()
        self.addCleanup(p.stop)
        p = patch('pr_review.issue_harness.invoke', side_effect=self.harness)
        p.start()
        self.addCleanup(p.stop)

    def harness(self, session, state, batch, role, specification, feedback):
        from pr_review import owner_providers
        report = owner_providers.invoke(session, state, batch, role, {
            'feedback': session.items(state, batch['items']), 'scope': role == 'review',
            'task': {'rationale': 'specification'}, 'approved_issue_plan': {'plan': specification}})
        return {'status': 'ready' if report['complete'] and not report['blocking'] else
                'working' if role == 'fix' else 'changes_requested',
                'outcomes': report.get('outcomes', []),
                'summary': report['summary'], 'checks': report['checks'],
                'findings': report['blocking'], 'questions': []}

    def model(self, session, state, batch, role, context):
        self.calls.append((role, copy.deepcopy(context)))
        if role == 'plan':
            completed = context['completed_tasks']
            tasks = ([task('whole', self.key)] if self.legacy else
                     [task('pipeline', self.key)] if completed else
                     [task('config', self.key), task('pipeline', self.key, ['config'])])
            return {'tasks': tasks}
        name = context.get('task', {}).get('rationale')
        incomplete = (role == 'fix' and (self.legacy or name == self.blocked_task)
                      or role == 'review' and getattr(self, 'incomplete_review', False))
        if role == 'fix' and not incomplete:
            path = Path(state['worktree']) / (name + '.txt')
            path.write_text(name+' implementation\n')
            batch['checkpoint'] = owner_git.checkpoint(session, state, 'test step')
        blocked = incomplete or (context.get('scope') and self.final_failure)
        return {'summary': 'Work remains' if blocked else 'Verified', 'complete': not incomplete,
                'outcomes': [{'item': self.key, 'status': 'no_change' if incomplete else 'fixed', 'evidence': 'Checked scope'}],
                'blocking': ['Implement remaining scope'] if blocked else [], 'checks': []}

    def start(self, **fields):
        return self.fixture.request('owner-fix', items=[self.key], **fields)

    def flow(self):
        flow = object.__new__(Workflow)
        flow.state = {'issue': 12, 'viewer': 'author'}
        flow.facts = {'comments': []}
        flow.request = {}
        flow.save = Mock()
        flow.writes = Mock()
        return flow

    def test_steps_receive_scope_and_final_review_assesses_issue_once(self):
        view = self.start(auto_commit=True)
        batch = view['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertEqual([self.key], batch['verified_items'])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        self.assertEqual(batch['commit_progress']['commits'], batch['commit_progress']['items'][self.key])
        partial = owner_commit_plan.inspect(self.owner, batch, batch['commit_progress']['commits'][0])
        self.assertNotIn(self.key, partial['items'])
        task_reviews = [c for role,c in self.calls if role == 'review' and not c.get('scope')]
        self.assertEqual(['config', 'pipeline'], [c['task']['rationale'] for c in task_reviews])
        final = [c for role,c in self.calls if role == 'review' and c.get('scope')]
        self.assertEqual(1, len(final))
        self.assertEqual([self.key], [i['key'] for i in final[0]['feedback']])
        self.assertEqual(['Both work together'], final[0]['approved_issue_plan']['plan']['acceptance'])
        self.assertFalse(self.fixture.posts)

    def test_partial_issue_resume_reuses_completed_step(self):
        self.blocked_task = 'pipeline'
        view = self.start()
        batch = view['batches'][-1]
        self.assertEqual(['complete', 'needs_attention'], [t['status'] for t in batch['tasks']])
        self.assertFalse(any(c.get('scope') for _,c in self.calls))
        self.calls.clear()
        self.blocked_task = None
        view = self.fixture.request('owner-fix', batch=batch['id'], replan=True, auto_commit=True)
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(['pipeline'], [c['task']['rationale'] for role,c in self.calls if role == 'fix'])
        replanning = next(c for role, c in self.calls if role == 'plan')
        self.assertEqual(['pipeline'], [t['rationale'] for t in replanning['unfinished_tasks']])
        self.assertEqual(['Implement remaining scope'],
                         replanning['unfinished_tasks'][0]['implementation']['blocking'])
        self.assertNotIn('before', replanning['unfinished_tasks'][0])
        self.assertEqual(2, len(view['batches'][-1]['commit_progress']['commits']))

    def test_failed_final_review_never_commits_partial_issue(self):
        self.final_failure = True
        # Keep the final repair report incomplete so the final verifier cannot pass.
        original = self.model
        def invoke(*args):
            if args[3] == 'fix' and args[4].get('scope'):
                return {'summary':'Integration unresolved', 'complete':False, 'blocking':['Integration unresolved'],
                        'checks':[], 'outcomes':[{'item':self.key,'status':'no_change','evidence':'Unresolved'}]}
            return original(*args)
        with patch('pr_review.owner_providers.invoke', side_effect=invoke):
            view = self.start(auto_commit=True)
        batch = view['batches'][-1]
        self.assertEqual('paused', batch['status'])
        self.assertEqual(batch['baseline'], self.fixture.git('rev-parse','HEAD',cwd=view['worktree']))
        self.assertFalse(self.fixture.request('owner-prepare-commit')['ready'])

    def test_skipped_issue_step_cannot_pass_final_review(self):
        self.blocked_task = 'pipeline'
        view = self.start()
        batch = view['batches'][-1]
        self.calls.clear()
        view = self.fixture.request('owner-fix', batch=batch['id'], skip=[batch['tasks'][1]['id']])
        self.assertEqual('paused', view['batches'][-1]['status'])
        self.assertFalse(any(c.get('scope') for _,c in self.calls))

    def test_manual_issue_selection_prepares_first_step(self):
        view = self.start()
        prepared = self.fixture.request('owner-prepare-commit', item=self.key)
        self.assertTrue(prepared['ready'])
        self.assertEqual((1, 2), (prepared['position'], prepared['total']))

    def test_issue_error_exposes_assessed_blocker_and_posts_status(self):
        self.blocked_task = 'config'
        self.incomplete_review = True
        flow = self.flow()
        view = self.start()
        view['batches'][-1]['commit_execution'] = {'error': 'Commit hook failed'}
        with patch('pr_review.owner_fixes.execute', return_value=view), self.assertRaises(ApiError) as caught:
            flow.fix(self.owner, [self.key])
        self.assertIn('Commit hook failed', str(caught.exception))
        self.assertEqual('commit', caught.exception.details['stage'])
        self.assertEqual('inspect_owner', caught.exception.details['next_action'])
        self.assertEqual(['Commit hook failed'], caught.exception.details['blocking'])
        body, key = flow.writes.comment.call_args.args
        self.assertIn('execution stop, not a request for plan approval', body)
        self.assertNotIn('CLI', body)
        self.assertIn('Commit hook failed', body)
        self.assertTrue(key.startswith('owner-pause:'))
        self.assertNotIn('execution_blockers', flow.state)

    def test_notice_failure_preserves_owner_diagnostics(self):
        self.blocked_task = 'config'
        self.incomplete_review = True
        flow = self.flow()
        flow.writes.comment.side_effect = ApiError('uncertain', 'Read-back failed')
        view = self.start()
        view['batches'][-1]['commit_execution'] = {'error': 'Commit hook failed'}
        with patch('pr_review.owner_fixes.execute', return_value=view), self.assertRaises(ApiError) as caught:
            flow.fix(self.owner, [self.key])
        self.assertEqual('uncertain', caught.exception.code)
        self.assertEqual('commit', caught.exception.details['owner_failure']['stage'])


if __name__ == '__main__':
    unittest.main()
