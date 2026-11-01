"""Repair verification dependency cycles without losing evidence obligations."""
import copy
import unittest
from unittest.mock import patch

import test_issue_steps
from pr_review import owner_plan_recovery as recovery
from pr_review.common import ApiError


def case(identity):
    return {'id': identity, 'scenario': identity, 'expected': 'Verify ' + identity,
            'basis': 'Approved issue', 'intent': 'preserve'}


class PlanRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issue_steps.IssueStepsTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.proposals = []
        self.fail_plan = False
        original = self.t.model
        def model(session, state, batch, role, context):
            report = original(session, state, batch, role, context)
            if role == 'plan':
                if context.get('planning_repair'):
                    proposal = copy.deepcopy(context['planning_repair'])
                    self.proposals.append(proposal)
                    if self.fail_plan:
                        raise ApiError('provider_failed', 'Planner interrupted')
                    task = test_issue_steps.task('integration', self.t.key)
                    task['expected'][0]['outcome'] = '\n'.join(r['outcome'] for r in proposal['requirements'])
                    task['verification_cases'] = [r['case'] for r in proposal['retained_cases']]
                    return {'tasks': [task]}
                tasks = [test_issue_steps.task('config', self.t.key),
                         test_issue_steps.task('migration', self.t.key, ['config']),
                         test_issue_steps.task('execution', self.t.key, ['migration'])]
                for t in tasks:
                    t['verification_cases'] = [case(t['id'])]
                return {'tasks': tasks}
            required = context.get('verification_cases', [])
            report['case_results'] = [{'id': c['id'], 'status': 'tested', 'evidence': 'Focused check passed'} for c in required]
            if role == 'fix':
                report['verification_cases'] = []
            if role == 'review' and context.get('task', {}).get('rationale') == 'migration':
                report.update(assessment='incomplete', complete=False, blocking=['Resume needs downstream execution'])
                report['case_results'][0]['status'] = 'unavailable'
                report['plan_dependencies'] = [{'case_id': 'migration', 'requires_task': 'p1-execution',
                                               'evidence': 'The execution entry point is implemented by that later task.'}]
            return report
        p = patch('pr_review.owner_providers.invoke', side_effect=model)
        p.start()
        self.addCleanup(p.stop)

    def start(self):
        return self.t.start(auto_commit=True, automatic_recovery=True)

    def test_cycle_repaired_and_completed_task_commit_preserved(self):
        view = self.start()
        batch = view['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertEqual(1, len(self.proposals))
        self.assertEqual(2, len(batch['plans']))
        old = batch['plans'][-1]['previous_tasks']
        self.assertEqual(old[0], {k: v for k, v in batch['tasks'][0].items() if k != 'verification_superseded'})
        self.assertEqual(['p1-config', 'p2-integration'], [t['id'] for t in batch['tasks']])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        self.assertTrue(batch['review']['complete'])
        self.assertEqual(3, len(batch['review']['case_results']))
        self.assertEqual('applied', self.t.owner.load()['issue_plan_recovery']['status'])
        self.assertNotIn('issue_recovery', self.t.owner.load())

    def test_failed_planning_reservation_is_not_reset_on_resume(self):
        self.fail_plan = True
        with self.assertRaises(ApiError):
            self.start()
        state = self.t.owner.load()
        self.assertEqual('failed', state['issue_plan_recovery']['status'])
        self.fail_plan = False
        self.t.fixture.request('owner-fix', batch=state['batches'][-1]['id'])
        self.assertEqual(1, len(self.proposals))

    def test_saved_legacy_stop_resumes_without_resetting_repair_credits(self):
        with patch('pr_review.owner_plan_recovery.reserve', return_value=None):
            stopped = self.start()
        state = self.t.owner.load()
        batch = state['batches'][-1]
        batch['tasks'][1]['review'].pop('plan_dependencies')
        batch['tasks'][1]['repairs'] = 1
        budget = {'limit': 2, 'attempts': [{'status': 'rejected'}, {'status': 'rejected'}], 'authorizations': []}
        state['issue_recovery'] = copy.deepcopy(budget)
        self.t.owner.save(state)
        view = self.t.fixture.request('owner-fix', batch=batch['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(stopped['batches'][-1]['id'], view['batches'][-1]['id'])
        self.assertEqual(budget, self.t.owner.load()['issue_recovery'])
        self.assertEqual(1, view['batches'][-1]['tasks'][1]['repairs'])
        self.assertEqual(1, len([c for role, c in self.t.calls if role == 'fix'
                                 and c.get('task', {}).get('rationale') == 'migration']))

    def test_changed_or_missing_obligations_rejected(self):
        view = self.start()
        proposal = self.proposals[0]
        tasks = copy.deepcopy(view['batches'][-1]['tasks'][1:])
        recovery.validate(tasks, proposal)
        for field in ('expected', 'scenario', 'basis', 'intent', 'id'):
            changed = copy.deepcopy(tasks)
            changed[0]['verification_cases'][0][field] = 'Changed'
            with self.assertRaises(ApiError):
                recovery.validate(changed, proposal)
        changed = copy.deepcopy(tasks)
        changed[0]['expected'][0]['outcome'] = 'Less work'
        with self.assertRaises(ApiError):
            recovery.validate(changed, proposal)
        reordered = []
        for i, requirement in enumerate(proposal['requirements']):
            task = test_issue_steps.task('step' + str(i), self.t.key, ['step0'] if i else [])
            task['expected'][0]['outcome'] = requirement['outcome']
            task['verification_cases'] = [r['case'] for r in proposal['retained_cases'] if r['task'] == requirement['task']]
            reordered.append(task)
        with self.assertRaisesRegex(ApiError, 'still precedes'):
            recovery.validate(reordered, proposal)

    def test_unknown_dependency_cannot_trigger_replanning(self):
        view = self.start()
        batch = copy.deepcopy(view['batches'][-1])
        batch['tasks'] = batch['plans'][-1]['previous_tasks']
        task = batch['tasks'][1]
        state = self.t.owner.load()
        for dependency in ('p1-config', 'missing', 'p1-migration'):
            task['review']['plan_dependencies'][0]['requires_task'] = dependency
            with self.assertRaises(ApiError):
                recovery.proposal(state, batch, task)
