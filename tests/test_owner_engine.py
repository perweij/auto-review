"""Interactive Owner final verification through the real shared engine.

Per-task checks stay one lightweight ``owner_providers.invoke(role='review')`` call;
final verification is the engine with scripted providers, real Git and the unchanged guards.
"""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_issue_steps
from engine_fixture import ScriptedProviders, finding
from pr_review import owner_commit, owner_contracts, owner_git, owner_state
from pr_review.common import ApiError


class OwnerEngineTest(ScriptedProviders, unittest.TestCase):
    workspace_name = 'owner-final'

    def setUp(self):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.base = self.f.fixture
        self.keys = [self.f.key]
        self.session = self.f.owner
        self.install_providers()
        self.use(final_review={'reviewer_slots': [{'id': 'reviewer-1', **self.SYNTHESIS}],
                               'synthesis': dict(self.SYNTHESIS), 'synthesis_tests': 'focused'})
        self.verdict()
        real = self.f.model

        def model(session, state, batch, role, context):
            os.environ['OWNER_WORKTREE'] = state['worktree']
            if role == 'fix' and context.get('scope'):         # the one integration repair
                self.repairs.append(context.get('review_findings'))
                (Path(state['worktree']) / 'integration.txt').write_text('integration repair\n')
                batch['checkpoint'] = owner_git.checkpoint(session, state, 'integration repair')
                return {'summary': 'Integrated', 'complete': True, 'checks': [], 'blocking': [],
                        'outcomes': [{'item': self.f.key, 'status': 'fixed', 'evidence': 'repaired'}], 'case_results': []}
            return real(session, state, batch, role, context)
        self.repairs = []
        patcher = patch('pr_review.owner_providers.invoke', side_effect=model)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, **changes):
        state = self.session.load()
        state['settings'].update(changes)
        self.session.save(state)

    def sync(self):
        self.state = self.session.load()
        self.batch = self.state['batches'][-1]
        return self.batch

    def candidate_sha(self):
        batch = self.sync()
        return owner_git.review_commit(self.session, batch['checkpoint']['tree'], batch['baseline'])['sha']

    def synthesis_runs(self):
        """Every synthesis run for every candidate of this session."""
        total = 0
        for path in (self.session.path / 'review-engine').glob('*/owner-final/logs/commands.json'):
            total += sum(entry['stage'] == 'synthesis' for entry in json.loads(path.read_text()))
        return total

    # ---- the real engine is the final review -----------------------------------------------------------
    def test_task_checks_stay_single_lightweight_calls_and_final_verification_uses_the_engine(self):
        view = self.f.start(auto_commit=True)
        batch = view['batches'][-1]
        self.sync()
        task_checks = [c for role, c in self.f.calls if role == 'review']
        self.assertEqual(['config', 'pipeline'], [c['task']['rationale'] for c in task_checks])
        self.assertFalse(any(c.get('scope') for c in task_checks))      # the final review is not an invoke() call
        self.assertEqual(1, self.counts()['synthesis'])
        prompt = self.stdin_copy.read_text()
        self.assertIn('claims to assess, not\nevidence', prompt)
        self.assertNotIn('implementation_test_evidence', prompt)
        self.assertEqual(1, batch['review_engine'])
        self.assertTrue(owner_contracts.current(batch))
        self.assertEqual(['p1-config', 'p1-pipeline'], [t['id'] for t in batch['tasks']])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        attempt = next(a for a in batch['attempts'] if a.get('kind') == 'review_engine')
        self.assertEqual(('final_review', 'complete'), (attempt['role'], attempt['status']))
        # The approved plan reached the engine as a context file.
        context = json.loads((self.workspace() / 'context/approved_issue_plan.json').read_text())
        self.assertEqual(['Both work together'], context['plan']['acceptance'])

    def test_unrelated_description_reuses_completed_engine_results(self):
        batch = self.f.start()['batches'][-1]
        counts = self.counts()
        calls = len(self.f.calls)
        self.session.repo.git('config', 'branch.unrelated.description', 'Another task')
        result = self.f.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(counts, self.counts())
        self.assertEqual(calls, len(self.f.calls))

    def test_non_blocking_observations_do_not_stop_an_owner_pass(self):
        self.verdict(findings=[finding('F1', blocking=False, source='new')])
        batch = self.f.start(auto_commit=True)['batches'][-1]
        self.assertEqual([], batch['review']['blocking'])
        self.assertTrue(owner_contracts.current(batch))
        artifact = json.loads((self.workspace() / 'reviews/synthesis.json').read_text())
        self.assertEqual(['F1'], [f['id'] for f in artifact['findings']])

    def test_final_verification_enforces_outcome_coverage(self):
        self.verdict(outcomes=[])
        with self.assertRaises(ApiError) as caught:
            self.f.start()
        self.assertEqual('invalid_result', caught.exception.code)
        batch = self.sync()
        self.assertEqual('paused', batch['status'])
        self.assertNotIn('final_tree', batch)

    def test_final_verification_enforces_case_coverage_for_every_combined_case(self):
        real = self.f.model
        case = {'id': 'explicit', 'scenario': 'Scenario', 'expected': 'Expected', 'basis': 'Basis', 'intent': 'change'}
        def model(session, state, batch, role, context):
            report = real(session, state, batch, role, context)
            required = context.get('verification_cases', [])
            if role == 'plan':
                for task in report['tasks']:
                    task['verification_cases'] = [case]
            elif role in ('fix', 'review'):
                report['case_results'] = [{'id': c['id'], 'status': 'tested', 'evidence': 'task-level evidence'} for c in required]
                if role == 'fix':
                    report['verification_cases'] = []
            return report
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            self.verdict(cases=[{'id': 'p1-config/explicit', 'status': 'tested', 'evidence': 'ran'}])   # p1-pipeline is missing
            with self.assertRaises(ApiError) as caught:
                self.f.start()
            self.assertEqual('invalid_result', caught.exception.code)
            self.assertNotIn('final_tree', self.sync())
            self.verdict(cases=[{'id': f'p1-{t}/explicit', 'status': 'unavailable', 'evidence': 'no tool'} for t in ('config', 'pipeline')])
            with self.assertRaises(ApiError):
                self.f.start(batch=self.sync()['id'])                    # unavailable evidence cannot support a pass
            self.verdict(cases=[{'id': f'p1-{t}/explicit', 'status': 'tested', 'evidence': 'ran'} for t in ('config', 'pipeline')])
            view = self.f.start(batch=self.sync()['id'])
        batch = view['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertTrue(owner_contracts.current(batch))
        self.assertEqual(['p1-config/explicit', 'p1-pipeline/explicit'], [r['id'] for r in batch['review']['case_results']])

    # ---- bounded repair and read-only verification ----------------------------------------------------------
    def test_one_bounded_repair_follows_a_rejection_and_a_second_rejection_pauses(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new', severity='High')])
        view = self.f.start()
        batch = view['batches'][-1]
        self.assertEqual('paused', batch['status'])
        self.assertEqual(1, batch['repairs'])
        self.assertEqual(1, len(self.repairs))
        self.assertIn('Finding F1', ' '.join(self.repairs[0]['blocking']))
        self.assertEqual(2, self.synthesis_runs())                       # the original and the repaired candidate
        self.assertNotIn('final_tree', batch)
        self.assertEqual('changes_requested', batch['review']['assessment'])

    def test_a_repair_that_passes_makes_the_new_candidate_current(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        original = self.repairs

        class Repairs(list):
            """The repair turn itself switches the scripted verdict to a pass."""
            def append(inner, value):
                super().append(value)
                self.verdict('passed')
        self.repairs = Repairs()
        view = self.f.start()
        batch = view['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertEqual(1, len(self.repairs))
        self.assertTrue(owner_contracts.current(batch))
        self.assertEqual(batch['final_tree'], batch['review_tree'])
        self.assertEqual(2, self.synthesis_runs())
        self.assertTrue((Path(batch_path(self)) / 'integration.txt').exists())

    def test_verify_only_uses_the_engine_but_never_repairs(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        view = self.f.start(verify_only=True)
        batch = view['batches'][-1]
        self.assertTrue(batch['verify_only'])
        self.assertEqual('paused', batch['status'])
        self.assertEqual([], self.repairs)
        self.assertEqual(1, self.counts()['synthesis'])

    def test_verify_only_can_pass(self):
        batch = self.f.start(verify_only=True)['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertEqual(1, self.counts()['synthesis'])

    # ---- frozen settings, markers and orphaned stages -----------------------------------------------------------
    def test_final_review_settings_are_frozen_once_model_work_has_started(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        batch = self.f.start(verify_only=True)['batches'][-1]
        changed = copy.deepcopy(self.session.load()['settings'])
        changed['final_review']['synthesis']['model'] = 'another-model'
        self.base.request('owner-mutate', operation='settings', settings=changed)
        with self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'], verify_only=False)
        self.assertEqual('settings_changed', caught.exception.code)

    def test_a_started_batch_keeps_the_final_review_settings_it_started_with(self):
        self.f.start(verify_only=True)
        batch = self.sync()
        self.assertEqual(self.SYNTHESIS, batch['settings']['final_review']['synthesis'])

    def test_an_orphaned_engine_stage_resumes_without_adoption_when_the_source_is_unchanged(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        batch = self.f.start(verify_only=True)['batches'][-1]
        self.verdict('passed')
        state = self.session.load()
        saved = state['batches'][-1]
        saved['status'] = 'reviewing'                      # a crash left the stage running
        saved['attempts'].append({'role': 'final_review', 'kind': 'review_engine', 'status': 'running', 'path': 'x'})
        self.session.save(state)
        view = self.base.request('owner-fix', batch=batch['id'])
        resumed = view['batches'][-1]
        self.assertEqual('ready', resumed['status'])
        self.assertTrue(any(a['kind'] == 'review_engine' and a['status'] == 'interrupted' for a in resumed['attempts']))
        self.assertTrue(owner_contracts.current(resumed))

    def test_source_changes_after_an_orphaned_engine_stage_still_require_adoption(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        batch = self.f.start(verify_only=True)['batches'][-1]
        state = self.session.load()
        state['batches'][-1]['attempts'].append({'role': 'final_review', 'kind': 'review_engine', 'status': 'running', 'path': 'x'})
        self.session.save(state)
        (Path(state['worktree']) / 'manual.txt').write_text('manual edit\n')
        with self.assertRaises(ApiError) as caught:
            self.base.request('owner-fix', batch=batch['id'])
        self.assertEqual('worktree_changed', caught.exception.code)

    def test_an_unfinished_batch_from_before_the_engine_stops_with_its_artifacts_kept(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        batch = self.f.start(verify_only=True)['batches'][-1]
        state = self.session.load()
        state['batches'][-1].pop('review_engine')
        self.session.save(state)
        before = self.session.load()['batches'][-1]
        with self.assertRaises(ApiError) as caught:
            self.base.request('owner-fix', batch=batch['id'])
        self.assertEqual('unsupported_session', caught.exception.code)
        self.assertEqual(before, self.session.load()['batches'][-1])
        self.assertTrue(Path(self.session.load()['worktree']).is_dir())
        self.assertTrue(self.workspace().is_dir())

    def test_a_ready_batch_from_before_the_engine_can_still_commit(self):
        batch = self.f.start()['batches'][-1]
        self.assertEqual('ready', batch['status'])
        state = self.session.load()
        state['batches'][-1].pop('review_engine')
        self.session.save(state)
        prepared = self.base.request('owner-prepare-commit')
        self.assertTrue(prepared['ready'], prepared)

    def test_an_untouched_batch_adopts_current_settings_and_the_marker(self):
        state = self.session.load()
        state['batches'].append({'id': 'untouched', 'status': 'paused', 'attempts': [], 'tasks': [], 'plans': [],
                                 'items': [self.f.key]})
        from pr_review import issue_harness
        issue_harness.require_engine(state['batches'][-1])
        self.assertEqual(1, state['batches'][-1]['review_engine'])

    # ---- legacy settings -----------------------------------------------------------------------------------
    def test_a_session_without_final_review_stays_valid_and_reviews_with_its_single_task_check_choice(self):
        state = self.session.load()
        state['settings'].pop('final_review')
        state['settings']['review'] = dict(self.SYNTHESIS, model='legacy-review-model')
        self.session.save(state)
        batch = self.f.start()['batches'][-1]
        self.assertEqual('ready', batch['status'])
        self.assertNotIn('final_review', self.session.load()['settings'])          # never written back
        self.assertNotIn('final_review', batch['settings'])
        self.assertEqual({'synthesis': 1}, self.counts())
        phase = json.loads((self.workspace() / 'manifest.json').read_text())['phases']['synthesis']
        self.assertEqual('legacy-review-model', phase['model'])
        view = self.session.view()
        self.assertEqual('legacy-review-model', view['effective_settings']['final_review']['synthesis']['model'])
        self.assertEqual('legacy-review-model', view['batches'][-1]['effective_settings']['final_review']['reviewer_slots'][0]['model'])


if __name__ == '__main__':
    unittest.main()


def batch_path(case):
    return case.session.load()['worktree']
