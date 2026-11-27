"""Evaluation regressions against the current owner workflow, with real Git."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_owner as fixtures
from pr_review import owner_git, owner_fixes
from pr_review.api import dispatch
from pr_review.common import ApiError
from pr_review.owner_state import OwnerSession
from pr_review.processes import run as process_run


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.t = fixtures.OwnerTest()
        self.addCleanup(self.t.doCleanups)
        self.t.setUp()

    def split(self, *args):
        if args[3] == 'plan':
            return {'decision': 'repair', 'basis': 'Use a targeted fix', 'tasks': [dict(id='t' + str(i), items=[item['key']], depends_on=[],
                                   rationale='Independent fix', expected=[dict(item=item['key'], outcome='fixed')])
                              for i, item in enumerate(args[4]['feedback'])]}
        return self.t.model(*args)

    def test_exhausted_saved_group_finishes_in_one_automatic_resume(self):
        self.resume_exhausted_group(legacy=False)

    def test_resume_reclassifies_legacy_blockers_before_another_fix(self):
        self.resume_exhausted_group(legacy=True)

    def resume_exhausted_group(self, *, legacy):
        self.t.feedback = [self.t.item(f'thread:T{i}', 100+i) for i in range(1, 8)]
        def model(session, state, batch, role, data):
            if role == 'plan':
                return self.split(session, state, batch, role, data)
            result = self.t.model(session, state, batch, role, data)
            if role == 'fix':
                path = Path(state['worktree']) / data['feedback'][0]['key'].replace(':', '-')
                path.write_text('fixed')
                batch['checkpoint'] = owner_git.checkpoint(session, state, 'group fix')
            if role == 'review' and data.get('task', {}).get('id') == 'p1-t2' and not batch.get('auto_commit'):
                result['blocking'] = ['foreign inventory', 'retry invalid page']
                if not legacy:
                    result['repair_basis'] = [dict(blocker=b, kind='feedback', reference=data['feedback'][0]['key'],
                        source='Selected inventory concern', scenario='Retry a saved page', impact=b)
                        for b in result['blocking']]
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix()['batches'][-1]
            owner = OwnerSession({'session': self.t.path})
            state = owner.load()
            task = state['batches'][-1]['tasks'][2]
            self.assertEqual(task['status'], 'needs_attention')
            task['repairs'] = 2
            owner.save(state)
            batch = self.t.request('owner-fix', batch=batch['id'], auto_commit=True)['batches'][-1]
        self.assertEqual(batch['commit_execution']['status'], 'complete')
        self.assertEqual([t['status'] for t in batch['tasks']], ['complete'] * 7)
        if legacy:
            self.assertFalse(batch['tasks'][2].get('recovery_attempts'))
            self.assertEqual([], batch['tasks'][2]['review']['blocking'])
            return
        evidence = batch['tasks'][2]['recovery_attempts'][0]['evidence']
        self.assertEqual(evidence['blocking'], ['foreign inventory', 'retry invalid page'])

    def test_repeated_failure_is_set_aside_before_independent_task(self):
        def model(session, state, batch, role, data):
            if role == 'plan':
                return self.split(session, state, batch, role, data)
            first = data['feedback'][0]['key'] == 'thread:T1'
            if role == 'fix' and not first:
                self.assertFalse(Path(state['worktree'], 'new.txt').exists())
            result = self.t.model(session, state, batch, role, data)
            if role == 'review' and first:
                result['blocking'] = ['Same failing behavior']
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            view = self.t.fix(auto_commit=True)
        batch = view['batches'][-1]
        self.assertIn('Repair loop', batch['error'])
        self.assertEqual([t['status'] for t in batch['tasks']], ['needs_attention', 'complete'])
        self.assertFalse(view['replies'])
        self.t.calls.clear()
        self.t.request('owner-fix', batch=batch['id'])
        self.assertFalse(self.t.calls)

    def test_new_blockers_each_round_converge_on_passing_cases_after_plan_budget(self):
        # Each review reports a new state of the same behavior, so no signature repeats.
        rounds = iter(range(100))
        budgets, fixes = [], []
        def model(session, state, batch, role, data):
            if role == 'plan':
                if data.get('automatic_recovery'):
                    budgets.append(data['automatic_recovery']['plan_budget'])
                    self.assertNotIn(owner_fixes.COMBINED_COVERAGE, data['automatic_recovery']['instruction'])
                return self.split(session, state, batch, role, data)
            if role == 'fix':
                fixes.append(data)
            result = self.t.model(session, state, batch, role, data)
            if role == 'review' and not data.get('scope') and data['feedback'][0]['key'] == 'thread:T1':
                result['blocking'] = [f'Wrong retry advice in state {next(rounds)}']
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        limit = owner_fixes.MAX_RECOVERY_PLANS
        self.assertEqual([dict(this_plan=n, limit=limit) for n in range(1, limit + 1)], budgets)
        self.assertFalse(any('convergence' in data for data in fixes))
        task = batch['tasks'][0]
        self.assertEqual(['complete', 'complete'], [t['status'] for t in batch['tasks']])
        self.assertEqual(len(task['recovery_attempts']), limit)
        deferred = task['review']['deferred_after_plan_budget']
        self.assertEqual(1, len(deferred))
        self.assertIn(deferred[0], batch['scope_observations'])
        self.assertEqual('ready', batch['status'])
        self.assertNotIn('error', batch)

    def test_a_required_case_that_keeps_failing_stops_after_converging_repairs(self):
        rounds = iter(range(100))
        case = dict(id='retry-advice', scenario='Exhaust memory on resume', expected='Resume advice',
                    basis='Selected feedback', intent='change')
        converging = []
        def model(session, state, batch, role, data):
            if role == 'plan':
                plan = self.split(session, state, batch, role, data)
                plan['tasks'][0]['verification_cases'] = [case]
                return plan
            result = self.t.model(session, state, batch, role, data)
            first = data['feedback'][0]['key'] == 'thread:T1' and not data.get('scope')
            if role == 'fix':
                if data.get('convergence'):
                    converging.append(data['convergence'])
                result['case_results'] = [dict(id=c['id'], status='tested', evidence='Claimed')
                                          for c in data.get('verification_cases', [])]
            if role == 'review':
                result['case_results'] = [dict(id=c['id'], status='failed' if first else 'tested', evidence='Observed')
                                          for c in data.get('verification_cases', [])]
                if first:
                    result['blocking'] = [f'Retry advice still wrong in state {next(rounds)}']
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertEqual([owner_fixes.CONVERGENCE] * owner_fixes.MAX_CONVERGING_REPAIRS, converging)
        task = batch['tasks'][0]
        self.assertEqual('needs_attention', task['status'])
        self.assertIn('Required verification still failing after 3 converging repairs', batch['error'])
        self.assertEqual('complete', batch['tasks'][1]['status'])

    def test_incomplete_verification_retries_same_candidate(self):
        for stage in ('task', 'combined'):
            with self.subTest(stage=stage):
                reviews = []
                def model(session, state, batch, role, data):
                    result = self.t.model(session, state, batch, role, data)
                    if role == 'review' and bool(data.get('scope')) == (stage == 'combined'):
                        reviews.append(batch['checkpoint']['tree'])
                        if len(reviews) == 1:
                            result.update(complete=False, blocking=['Transient environment failure'])
                    return result
                with patch('pr_review.owner_providers.invoke', side_effect=model):
                    result = self.t.fix(auto_commit=True)
                self.assertEqual(result['batches'][-1]['status'], 'ready')
                self.assertEqual(len(reviews), 2)
                self.assertEqual(reviews[0], reviews[1])
                if stage == 'task':
                    self.t = fixtures.OwnerTest()
                    self.addCleanup(self.t.doCleanups)
                    self.t.setUp()

    def test_clarification_holds_dependents_and_preserves_independent_fix(self):
        self.t.feedback.append(self.t.item('thread:T3', 103))
        def model(session, state, batch, role, data):
            if role == 'plan':
                result = self.split(session, state, batch, role, data)
                if data.get('automatic_recovery'):
                    return {'tasks': [], 'decision': 'clarify', 'basis': 'Choose a retention period.'}
                result['tasks'][1]['depends_on'] = ['t0']
                return result
            result = self.t.model(session, state, batch, role, data)
            if role == 'fix' and data['task']['id'] == 'p1-t0':
                result['outcomes'][0].update(status='clarify', evidence='Which retention period?')
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertEqual([t['status'] for t in batch['tasks']], ['needs_attention', 'blocked', 'complete'])
        self.assertIn('Which retention period?', batch['error'])
        self.assertNotIn('commit_execution', batch)

    def test_persistent_final_prerequisite_retries_once_and_explains_stop(self):
        def model(*args):
            result = self.t.model(*args)
            if args[4].get('scope'):
                result.update(complete=False, blocking=['Missing project environment'])
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
            self.assertIn('Missing project environment', batch['recovery_stop'])
            self.assertEqual(sum(bool(data.get('scope')) for _, data in self.t.calls), 2)
            self.t.calls.clear()
            self.t.request('owner-fix', batch=batch['id'])
            self.assertFalse(self.t.calls)

    def test_final_retry_rejects_feedback_changed_since_view(self):
        def model(*args):
            report = self.t.model(*args)
            if args[4].get('scope'):
                report.update(complete=False, blocking=['Missing environment'])
            return report

        with patch('pr_review.owner_providers.invoke', side_effect=model):
            view = self.t.fix(auto_commit=True)
            batch = view['batches'][-1]
            self.assertTrue(batch['can_retry_final_recovery'])
            self.t.feedback[0]['comments'][0]['body'] = 'Revised concern'
            self.t.calls.clear()
            with self.assertRaises(ApiError) as caught:
                dispatch(dict(protocol=1, command='owner-fix', session=self.t.path,
                              revision=view['revision'], batch=batch['id'], retry_final_recovery=True))
        self.assertEqual('stale_discussion', caught.exception.code)
        current = self.t.request('owner-read')['batches'][-1]
        self.assertFalse(current['can_retry_final_recovery'])
        self.assertEqual(batch['tasks'], current['tasks'])
        self.assertEqual(batch['recovery_stop'], current['recovery_stop'])
        self.assertEqual(batch['error'], current['error'])
        self.assertNotIn('commit_execution', current)
        self.assertFalse(self.t.calls)

    def test_final_retry_preserves_skipped_and_completed_tasks(self):
        def incomplete(*args):
            report = self.split(*args)
            if args[4].get('scope'):
                report.update(complete=False, blocking=['Missing environment'])
            return report

        with patch('pr_review.owner_providers.invoke', side_effect=self.split):
            initial = self.t.fix()['batches'][-1]
        with patch('pr_review.owner_providers.invoke', side_effect=incomplete):
            batch = self.t.request('owner-fix', batch=initial['id'], auto_commit=True,
                                   skip=[initial['tasks'][0]['id']])['batches'][-1]
        self.assertTrue(batch['can_retry_final_recovery'])
        self.t.calls.clear()
        current = self.t.request('owner-fix', batch=batch['id'], retry_final_recovery=True)['batches'][-1]
        self.assertEqual('ready', current['status'])
        self.assertEqual([t['id'] for t in batch['tasks']], [t['id'] for t in current['tasks']])
        self.assertEqual(['skipped', 'complete'], [t['status'] for t in current['tasks']])
        self.assertEqual(['review'], [role for role, _ in self.t.calls])
        self.assertTrue(self.t.calls[0][1].get('scope'))

    def test_restart_before_alternate_fixer_uses_saved_plan(self):
        original = owner_fixes.dispatch_repair
        replans = []
        def model(*args):
            value = self.t.model(*args)
            role, data = args[3:]
            if role == 'plan' and data.get('automatic_recovery'):
                replans.append(data)
                value.update(decision='repair', basis='Use the saved failure')
            if role == 'review' and not replans:
                value['blocking'] = ['Regression remains']
            return value
        def interrupt(session, state, batch, target, data):
            if data.get('recovery_plan'):
                raise ApiError('cancelled', 'Stopped before dispatch')
            return original(session, state, batch, target, data)
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with patch.object(owner_fixes, 'dispatch_repair', side_effect=interrupt), self.assertRaises(ApiError):
                self.t.fix(auto_commit=True)
            batch = self.t.request('owner-read')['batches'][-1]
            final = self.t.request('owner-fix', batch=batch['id'])['batches'][-1]
        self.assertEqual(len(replans), 1)
        self.assertEqual(final['commit_execution']['status'], 'complete')
        self.assertEqual(sum(role == 'fix' and bool(data.get('recovery_plan'))
                             for role, data in self.t.calls), 1)

    def test_combined_recovery_carries_sibling_checks_with_frozen_contract(self):
        failures = iter(['Invalid request', 'Invalid v1 retry interval',
                         'Invalid legacy retry interval', None])
        plans, fixes = [], []
        sibling_check = 'Check retry intervals in both v1 and legacy inventories'
        def model(*args):
            value = self.t.model(*args)
            role, data = args[3:]
            if role == 'plan':
                value['tasks'][0]['verification_cases'] = [dict(id='interval', scenario='Read a saved page',
                    expected='Retries fall within the interval', basis='Inventory contract', intent='preserve')]
            else:
                value['case_results'] = [dict(id=case['id'], status='tested', evidence='Interval check passed')
                                         for case in data.get('verification_cases', [])]
            if role == 'review' and data.get('scope'):
                failure = next(failures)
                value['blocking'] = [failure] if failure else []
            if role == 'plan' and data.get('automatic_recovery'):
                plans.append(copy.deepcopy(data['automatic_recovery']))
                value.update(decision='repair', basis='Both formats certify retry intervals')
                value['tasks'][0]['rationale'] = sibling_check
            if role == 'fix' and data.get('recovery_plan'):
                fixes.append(copy.deepcopy(data))
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertTrue(all(owner_fixes.COMBINED_COVERAGE in plan['instruction'] for plan in plans))
        prior = plans[-1]['previous_approaches'][-1]['evidence']
        self.assertEqual(['Invalid v1 retry interval'], prior['blocking'])
        self.assertEqual(['Invalid legacy retry interval'], plans[-1]['findings']['blocking'])
        self.assertEqual(sibling_check, fixes[-1]['recovery_plan']['tasks'][0]['rationale'])
        self.assertEqual(1, len(fixes[0]['verification_cases']))
        self.assertEqual(fixes[0]['verification_cases'], fixes[-1]['verification_cases'])

    def test_alternating_final_regressions_stop_after_replanned_attempts(self):
        findings = iter(['rollback', 'retry', 'rollback', 'retry'])
        def model(*args):
            value = self.t.model(*args)
            if args[3] == 'plan' and args[4].get('automatic_recovery'):
                value.update(decision='repair', basis='Reconcile both scenarios')
            if args[3] == 'review' and args[4].get('scope'):
                value['blocking'] = [next(findings)]
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertIn('Repair loop', batch['error'])
        self.assertEqual(len(batch['recovery_attempts']), 2)
        self.assertNotIn('commit_execution', batch)

    def test_new_final_blockers_converge_after_combined_plan_budget(self):
        rounds = iter(range(100))
        def model(*args):
            value = self.t.model(*args)
            if args[3] == 'plan' and args[4].get('automatic_recovery'):
                value.update(decision='repair', basis='Cover the reported state')
            if args[3] == 'review' and args[4].get('scope'):
                value['blocking'] = [f'Diagnostic omits location in state {next(rounds)}']
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertEqual(len(batch['recovery_attempts']), owner_fixes.MAX_RECOVERY_PLANS)
        self.assertEqual(1, len(batch['review']['deferred_after_plan_budget']))
        self.assertEqual('complete', batch['commit_execution']['status'])

    def test_external_edit_before_setting_aside_a_candidate_is_preserved(self):
        def model(*args):
            result = self.split(*args)
            if args[3] == 'review':
                result.update(complete=False, blocking=['Missing tool'])
            return result
        restore = owner_git.restore_checkpoint
        def edit(session, state, batch, accepted):
            Path(state['worktree'], 'external.txt').write_text('keep this change')
            return restore(session, state, batch, accepted)
        with patch('pr_review.owner_providers.invoke', side_effect=model), patch.object(
                owner_git, 'restore_checkpoint', side_effect=edit), self.assertRaises(ApiError) as caught:
            self.t.fix(auto_commit=True)
        self.assertEqual(caught.exception.code, 'worktree_changed')
        view = self.t.request('owner-read')
        self.assertEqual(Path(view['worktree'], 'external.txt').read_text(), 'keep this change')
        self.assertNotIn('commit_execution', view['batches'][-1])

    def test_internal_error_is_identified_in_automatic_batch(self):
        with patch('pr_review.owner_providers.invoke', side_effect=KeyError('broken invariant')), self.assertRaises(KeyError):
            self.t.fix(auto_commit=True)
        batch = self.t.request('owner-read')['batches'][-1]
        self.assertIn('Internal Owner error', batch['error'])
        self.assertIn('broken invariant', batch['error'])

    def test_duplicate_outcome_retries_only_task_verification(self):
        checked = []
        def model(*args):
            report = self.t.model(*args)
            role, context = args[3:]
            if role == 'review' and context.get('task'):
                checked.append(args[2]['checkpoint']['tree'])
                if len(checked) == 1:
                    report['outcomes'][0]['item'] = report['outcomes'][1]['item']
                else:
                    self.assertIn('report_correction', context)
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix(auto_commit=True)['batches'][-1]
        self.assertEqual('complete', batch['commit_execution']['status'])
        self.assertEqual(2, len(checked))
        self.assertEqual(checked[0], checked[1])
        self.assertEqual(1, sum(role == 'fix' for role, _ in self.t.calls))

    def interrupted(self):
        def model(*args):
            value = self.split(*args)
            if args[3] == 'fix':
                raise ApiError('cancelled', 'Stopped after partial edits')
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with self.assertRaises(ApiError):
                self.t.fix()
        return self.t.request('owner-read')['batches'][-1]

    def test_unrelated_skip_cannot_restart_interrupted_task(self):
        batch = self.interrupted()
        self.t.calls.clear()
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-fix', batch=batch['id'], skip=[batch['tasks'][1]['id']])
        self.assertEqual(caught.exception.code, 'interrupted_task')
        self.assertFalse(self.t.calls)
        # Explicitly skipping the interrupted task still permits independent work.
        self.t.request('owner-fix', batch=batch['id'], skip=[batch['tasks'][0]['id']])
        self.assertTrue(any(role == 'fix' for role, _ in self.t.calls))

    def test_adoption_retains_queued_tasks_initial_fixer(self):
        batch = self.interrupted()
        self.t.calls.clear()
        self.t.request('owner-fix', batch=batch['id'], adopt=True)
        fixes = [c for role, c in self.t.calls if role == 'fix']
        self.assertEqual([[i['key'] for i in c['feedback']] for c in fixes], [['thread:T2']])

    def test_failed_preflight_preserves_ready_fix_for_commit_association(self):
        batch = self.t.fix()['batches'][-1]
        with patch('pr_review.owner_providers.preflight', side_effect=ApiError('authentication_failed', 'Offline')):
            with self.assertRaises(ApiError):
                self.t.request('owner-fix', batch=batch['id'])
        current = self.t.request('owner-read')['batches'][-1]
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(current['final_tree'], batch['final_tree'])
        self.t.commit_fix()
        self.assertEqual(len(self.t.request('owner-associate')['replies']), 2)

    def test_changed_feedback_does_not_preserve_ready_verification(self):
        batch = self.t.fix()['batches'][-1]
        self.t.feedback[0]['comments'][0]['body'] = 'New concern'
        with self.assertRaises(ApiError):
            self.t.request('owner-fix', batch=batch['id'])
        self.assertNotEqual(self.t.request('owner-read')['batches'][-1]['status'], 'ready')

    def test_skips_survive_replanning_and_deleted_items_can_be_excluded(self):
        with patch('pr_review.owner_providers.invoke', side_effect=self.split):
            batch = self.t.fix()['batches'][-1]
            self.t.feedback.pop(0)
            result = self.t.request('owner-fix', batch=batch['id'], replan=True, skip=[batch['tasks'][0]['id']])
            current = result['batches'][-1]
            self.assertEqual(current['status'], 'ready')
            self.assertEqual(current['verified_items'], ['thread:T2'])
            self.assertEqual(current['tasks'][0]['status'], 'skipped')
            self.assertFalse(self.t.request('owner-refresh')['batches'][-1]['discussion_stale'])
            self.t.calls.clear()
            self.t.request('owner-fix', batch=batch['id'], replan=True)
            self.assertFalse(any(i['key'] == 'thread:T1' for _, c in self.t.calls for i in c['feedback']))

    def test_replanning_preserves_skips_even_when_feedback_still_exists(self):
        with patch('pr_review.owner_providers.invoke', side_effect=self.split):
            batch = self.t.fix()['batches'][-1]
            self.t.calls.clear()
            current = self.t.request('owner-fix', batch=batch['id'], replan=True,
                                     skip=[batch['tasks'][0]['id']])['batches'][-1]
        self.assertEqual(current['tasks'][0]['status'], 'skipped')
        self.assertEqual(current['verified_items'], ['thread:T2'])
        self.assertFalse(any(i['key'] == 'thread:T1' for _, c in self.t.calls for i in c['feedback']))

    def test_replan_then_adopt_has_no_dangling_completed_dependencies(self):
        def model(*args):
            value = self.split(*args)
            if args[3] == 'plan' and len(value['tasks']) == 2:
                value['tasks'][1]['depends_on'] = ['t0']
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix()['batches'][-1]
            self.t.feedback[0]['comments'][0]['body'] = 'Changed first concern'
            self.t.request('owner-fix', batch=batch['id'], replan=True)
            current = self.t.request('owner-fix', batch=batch['id'], adopt=True)['batches'][-1]
        self.assertEqual(current['status'], 'ready')
        ids = {t['id'] for t in current['tasks']}
        self.assertTrue(all(set(t['depends_on']) <= ids for t in current['tasks']))

    def test_legacy_clarification_does_not_launch_task_or_combined_repairs(self):
        for combined in (False, True):
            with self.subTest(combined=combined):
                # Each case has an independent clean worktree and batch.
                if combined:
                    self.t.doCleanups()
                    self.t.setUp()
                def model(*args):
                    value = self.t.model(*args)
                    if args[3] == 'review' and bool(args[4].get('scope')) == combined:
                        value['blocking'] = ['Need an owner decision']
                        value['outcomes'][0]['status'] = 'clarify'
                    return value
                with patch('pr_review.owner_providers.invoke', side_effect=model):
                    view = self.t.fix()
                self.assertEqual(view['batches'][-1]['status'], 'paused')
                self.assertEqual(sum(role == 'fix' for role, _ in self.t.calls), 1)

    def test_failed_combined_repair_requires_adoption_with_actual_provider_wrapper(self):
        combined_reviews = []
        def fail_process(_argv, **kwargs):
            code = "from pathlib import Path; Path('partial.txt').write_text('retained'); raise SystemExit(1)"
            return process_run([sys.executable, '-c', code], **{**kwargs, 'timeout': 5})
        def model(*args):
            if args[3] == 'fix' and args[4].get('scope'):
                with patch('pr_review.owner_providers.run', side_effect=fail_process):
                    return fixtures.REAL_INVOKE(*args)
            value = self.t.model(*args)
            if args[3] == 'review' and args[4].get('scope'):
                combined_reviews.append(args[4])
                if len(combined_reviews) == 1:
                    value['blocking'] = ['Fix interaction']
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with self.assertRaises(ApiError):
                self.t.fix()
            batch = self.t.request('owner-read')['batches'][-1]
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-fix', batch=batch['id'])
            self.assertEqual(caught.exception.code, 'interrupted_task')
            self.assertEqual(len(combined_reviews), 1)
            current = self.t.request('owner-fix', batch=batch['id'], adopt=True)['batches'][-1]
            self.assertEqual(current['status'], 'ready')
            self.assertIn('partial.txt', combined_reviews[-1]['batch_diff'])
            self.assertEqual(self.t.request('owner-fix', batch=batch['id'])['batches'][-1]['status'], 'ready')

    def test_uncertain_reply_keeps_submitted_payload_during_reassociation(self):
        batch = self.t.fix()['batches'][-1]
        sha = self.t.commit_fix()
        view = self.t.request('owner-associate')
        original = dict(view['replies'][0])
        preview = self.t.request('owner-prepare')
        def uncertain(*args, **kwargs):
            self.t.publish(*args, **kwargs)
            raise ApiError('offline', 'Lost reply response')
        with patch('pr_review.publication.gh', side_effect=uncertain), self.assertRaises(ApiError):
            self.t.request('owner-publish', token=preview['token'])
        self.t.git('commit', '--amend', '-m', 'Amended fix', cwd=view['worktree'])
        amended = self.t.git('rev-parse', 'HEAD', cwd=view['worktree'])
        self.assertNotEqual(sha, amended)
        self.t.git('update-ref', 'refs/pull/7/head', amended)
        self.t.metadata['headRefOid'] = amended
        view = self.t.request('owner-associate', batch=batch['id'], commits=[amended])
        reply = next(r for r in view['replies'] if r['id'] == original['id'])
        self.assertEqual(reply['body'], original['body'])
        self.assertEqual(reply['commits'], [sha])
        view = self.t.request('owner-recover')
        reply = next(r for r in view['replies'] if r['id'] == original['id'])
        self.assertEqual(reply['status'], 'published')
        self.assertIn(sha, reply['receipt']['body'])

    def test_pr_closure_between_replies_preserves_receipts_and_stops_sending(self):
        for item in self.t.feedback:
            self.t.request('owner-mutate', operation='reply', item=item['key'], body='Reply')
        ids = [r['id'] for r in self.t.view['replies']]
        self.t.request('owner-mutate', operation='stage', replies=ids)
        preview = self.t.request('owner-prepare')
        def publish(*args, **kwargs):
            receipt = self.t.publish(*args, **kwargs)
            self.t.metadata['state'] = 'CLOSED'
            return receipt
        with patch('pr_review.publication.gh', side_effect=publish):
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-publish', token=preview['token'])
        self.assertEqual(caught.exception.code, 'closed_pr')
        self.assertEqual(len(self.t.posts), 1)
        self.assertEqual(sum(r['status'] == 'published' for r in self.t.request('owner-read')['replies']), 1)

    def test_missing_association_does_not_abort_surviving_reply(self):
        batch = self.t.fix()['batches'][-1]
        sha = self.t.commit_fix()
        self.t.request('owner-associate')
        self.t.feedback.pop(0)
        view = self.t.request('owner-associate', batch=batch['id'], commits=[sha], items=['thread:T2'])
        self.assertTrue(any(r['source'] == 'thread:T2' for r in view['replies']))
        self.assertTrue(any(i['item'] == 'thread:T1' for i in view['batches'][-1]['reply_issues']))

    def test_restored_acknowledged_general_comment_returns_to_attention(self):
        self.t.feedback.append(self.t.item('comment:3', 3, kind='comment'))
        self.t.request('owner-refresh')
        self.t.request('owner-mutate', operation='acknowledge', items=['comment:3'])
        view = self.t.request('owner-mutate', operation='restore', items=['comment:3'])
        item = next(i for i in view['feedback'] if i['key'] == 'comment:3')
        self.assertTrue(item['attention'])
        self.assertTrue(item['queued'])

    def test_confirmed_receipt_does_not_interrupt_two_replies_to_same_thread(self):
        self.t.fix()
        for body in ('First reply', 'Second reply'):
            self.t.request('owner-mutate', operation='reply', item='thread:T1', body=body)
        self.t.request('owner-mutate', operation='stage', replies=[r['id'] for r in self.t.view['replies']])
        preview = self.t.request('owner-prepare')
        def publish(*args, **kwargs):
            receipt = self.t.publish(*args, **kwargs)
            comment = copy.deepcopy(self.t.feedback[0]['comments'][0])
            comment.update(id=receipt['id'], author='author', body=receipt['body'])
            self.t.feedback[0]['comments'].append(comment)
            return receipt
        with patch('pr_review.publication.gh', side_effect=publish):
            self.t.request('owner-publish', token=preview['token'])
        self.assertEqual(len(self.t.posts), 2)
        self.assertFalse(self.t.request('owner-refresh')['batches'][-1]['discussion_stale'])

    def test_ambiguous_toolkit_selection_requires_repository_before_saving(self):
        self.t.git('remote', 'add', 'upstream', 'https://github.com/parent/repo.git')
        request = dict(protocol=1, command='toolkit', cwd=str(self.t.repo), toolkit='owner')
        with self.assertRaises(ApiError) as caught:
            dispatch(request)
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(dispatch({**request, 'repository': 'github.com/parent/repo'})['toolkit'], 'owner')

    def test_context_omits_identical_diff_and_bounds_ignored_inventory(self):
        def model(*args):
            value = self.t.model(*args)
            if args[3] == 'fix':
                session, state, batch = args[:3]
                root = Path(state['worktree'])
                (root / '.gitignore').write_text('generated/\n')
                (root / 'generated').mkdir()
                for i in range(60):
                    (root / 'generated' / str(i)).write_text('ignored')
                batch['checkpoint'] = owner_git.checkpoint(session, state, 'ignored fixtures')
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix()['batches'][-1]
        self.assertEqual(batch['checkpoint']['ignored_count'], 60)
        self.assertLessEqual(len(batch['checkpoint']['ignored']), 20)
        final = next(c for role, c in self.t.calls if role == 'review' and c.get('scope'))
        self.assertNotIn('task_diff', final)
        session = OwnerSession({'session': self.t.path})
        data = owner_fixes.context(session, session.load(), batch, batch['items'], batch['checkpoint']['tree'])
        self.assertEqual(data['task_diff'], '')  # Distinct empty task diff must remain.

    def test_checkpoint_refuses_dirty_submodule_contents(self):
        sub = self.t.root / 'module'
        sub.mkdir()
        self.t.git('init', '-q', '-b', 'main', cwd=sub)
        (sub / 'nested.txt').write_text('original')
        self.t.git('add', '.', cwd=sub)
        self.t.git('commit', '-qm', 'module', cwd=sub)
        self.t.git('-c', 'protocol.file.allow=always', 'submodule', 'add', str(sub), 'vendor')
        self.t.git('commit', '-qam', 'Add module')
        head = self.t.git('rev-parse', 'HEAD')
        self.t.git('update-ref', 'refs/pull/7/head', head)
        self.t.metadata['headRefOid'] = head
        self.t.request('owner-refresh')
        session = OwnerSession({'session': self.t.path})
        state = session.load()
        owner_git.prepare(session, state)
        worktree = Path(state['worktree'])
        self.t.git('-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', cwd=worktree)
        owner_git.checkpoint(session, state, 'clean module')
        (worktree / 'vendor/nested.txt').write_text('unsnapshotted change')
        with self.assertRaises(ApiError) as caught:
            owner_git.checkpoint(session, state, 'dirty module')
        self.assertEqual(caught.exception.code, 'dirty_submodule')
        self.assertEqual((worktree / 'vendor/nested.txt').read_text(), 'unsnapshotted change')
