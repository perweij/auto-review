"""The shared review engine against disposable Git fixtures and scripted providers.

Providers are small shell scripts. Everything else is real: Git, private workspaces, the
Owner checkpoint, the bridge commit, the disposable checkouts and the guards.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

import engine_fixture
from engine_fixture import EngineFixture, KEYS, finding
from pr_review import cache, acceptance, owner_commit, owner_contracts, owner_git, providers, review_engine, review_workspace
from pr_review.common import ApiError, read_json
from pr_review.processes import Cancelled
from pr_review.repository import Repository


class SlotTest(EngineFixture):
    def test_cleanup_permission_error_preserves_completed_reviews(self):
        from pr_review.isolation import IndependentWorkspace
        close = IndependentWorkspace.close
        leftovers = []

        def fail_cleanup(workspace):
            leftovers.append(workspace.temp)
            with patch.object(workspace.temp, 'cleanup', side_effect=PermissionError(1, 'denied', 17)):
                close(workspace)

        try:
            with patch.object(IndependentWorkspace, 'close', fail_cleanup):
                result = self.review()
            self.assertEqual('completed', result.status)
            self.assertTrue(leftovers)
            self.assertEqual('completed', self.manifest()['phases']['reviewer-1']['status'])
        finally:
            for temporary in leftovers:
                temporary.cleanup()

    def test_two_slots_have_independent_choices_and_a_separate_synthesis_choice(self):
        self.configure([{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'},
                        {'id': 'reviewer-2', 'provider': 'codex', 'model': 'gpt-a', 'effort': 'low'}],
                       {'provider': 'codex', 'model': 'gpt-synthesis', 'effort': 'medium'})
        result = self.review()
        self.assertEqual('completed', result.status)
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 1}, self.counts())
        phases = self.manifest()['phases']
        self.assertEqual(['opus', 'gpt-a', 'gpt-synthesis'], [phases[s]['model'] for s in ('reviewer-1', 'reviewer-2', 'synthesis')])
        workspaces = {c['stage']: c['cwd'] for c in self.commands()}
        self.assertNotEqual(workspaces['reviewer-1'], workspaces['reviewer-2'])
        argv = {c['stage']: c['argv'] for c in self.commands()}
        self.assertIn('--effort=high', argv['reviewer-1'])
        self.assertIn('--effort=low', argv['reviewer-2'])
        self.assertIn('--effort=medium', argv['synthesis'])
        self.assertEqual('private-snapshot-container-v1', phases['reviewer-1']['inputs']['isolation'])
        self.assertEqual('candidate-checkout-v1', phases['synthesis']['inputs']['isolation'])

    def test_two_slots_of_the_same_provider_stay_independent(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                self.candidate(content=f'edit for {provider}\n')
                self.configure([{'id': 'reviewer-1', 'provider': provider, 'model': 'model-a', 'effort': 'low'},
                                {'id': 'reviewer-2', 'provider': provider, 'model': 'model-b', 'effort': 'high'}],
                               self.SYNTHESIS)
                self.review()
                phases = self.manifest()['phases']
                self.assertEqual(['model-a', 'model-b'], [phases[s]['model'] for s in ('reviewer-1', 'reviewer-2')])
                self.assertNotEqual(phases['reviewer-1']['fingerprint'], phases['reviewer-2']['fingerprint'])
                workspaces = {c['stage']: c['cwd'] for c in self.commands()}
                self.assertNotEqual(workspaces['reviewer-1'], workspaces['reviewer-2'])
                reports = [(self.workspace() / f'reviews/reviewer-{n}.md').read_text() for n in (1, 2)]
                self.assertIn('stage=reviewer-1', reports[0])
                self.assertIn('stage=reviewer-2', reports[1])

    def test_local_candidate_content_reaches_private_and_synthesis_checkouts(self):
        self.review()
        marker = self.candidate_sha()[:12]
        for number in (1, 2):
            report = (self.workspace() / f'reviews/reviewer-{number}.md').read_text()
            self.assertIn('source=feature,local edit,', report)   # an uncommitted modification
            self.assertIn('new=new file,', report)                 # an untracked file
            self.assertIn(f'head={marker}', report)
            # Specification and feedback arrive only as files in the private root.
            self.assertIn('thread:T1', report)
            self.assertIn('selected_feedback.json', report)
        self.assertIn('content=feature,local edit,new file,', self.marks_text())
        self.assertIn(f'cwd={self.workspace() / "stages/synthesis/checkout"}', self.marks_text())

    def test_private_reviewer_editing_the_live_candidate_still_blocks_acceptance(self):
        original = self.command
        checkpoint = self.batch['checkpoint']
        def command(run, stage):
            argv = original(run, stage)
            if stage == 'reviewer-1':
                argv[2] = 'echo changed >> "$OWNER_WORKTREE/source.txt"\n' + argv[2]
            return argv
        with patch.object(review_workspace.CandidateReview, 'command', command), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('integrity_failed', caught.exception.code)
        batch = self.session.load()['batches'][-1]
        self.assertTrue(batch['integrity_failure'])
        self.assertEqual(checkpoint, batch['checkpoint'])
        self.assertNoAcceptance()

    def test_reviewers_get_only_their_own_context_and_checkout(self):
        self.review()
        workspace = self.workspace()
        for stage in ('reviewer-1', 'reviewer-2'):
            report = (workspace / f'reviews/{stage}.md').read_text()
            root_names = report.split('root=')[1].split(' files=')[0]
            self.assertIn('checkout,', root_names)
            for withheld in ('reviews,', 'session.json,', 'manifest.json,'):
                self.assertNotIn(withheld, root_names)
        self.assertEqual('feature\nlocal edit\n', (self.worktree / 'source.txt').read_text())

    def test_reviewers_receive_specification_and_feedback_but_nothing_from_the_implementer(self):
        self.data.update(verification_cases=[self.case()], approved_issue_plan={'plan': 'The approved plan'},
                         completed_tasks=[{'id': 't1', 'rationale': 'r', 'expected': [], 'implementation': {'summary': 'claim'}}])
        self.verdict()
        self.review()
        files = (self.workspace() / 'reviews/reviewer-1.md').read_text().split('files=')[1]
        for allowed in ('selected_feedback.json', 'approved_issue_plan.json', 'scope.json'):
            self.assertIn(allowed, files)
        for withheld in ('verification_cases', 'implementer_reports', 'earlier_findings'):
            self.assertNotIn(withheld, files)

    def test_synthesis_receives_both_reports_attributed_by_slot(self):
        self.review()
        prompt = self.stdin_copy.read_text()
        self.assertIn('Attribute each original finding to its reviewer slot ID', prompt)
        metadata = json.loads(prompt[prompt.index('{\n'):])
        reports = metadata['reviewer_reports']
        self.assertEqual([('reviewer-1', 'claude'), ('reviewer-2', 'codex')], [(r['slot'], r['provider']) for r in reports])
        self.assertTrue(all(Path(r['path']).is_file() for r in reports))
        self.assertIn('stage=reviewer-1', Path(reports[0]['path']).read_text())
        self.assertIn('stage=reviewer-2', Path(reports[1]['path']).read_text())
        self.assertEqual(KEYS, metadata['selected_keys'])

    def test_one_slot_differing_from_synthesis_has_its_own_workspace_and_synthesis_reads_its_report(self):
        self.configure([{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'}], self.SYNTHESIS)
        self.review()
        self.assertEqual({'reviewer-1': 1, 'synthesis': 1}, self.counts())
        self.assertIn('You are the processing reviewer', (self.workspace() / 'prompts/synthesis-prompt.txt').read_text())

    def test_claude_synthesis_returns_structured_output_events(self):
        self.single_reviewer()
        self.configure([{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'}],
                       {'provider': 'claude', 'model': 'opus', 'effort': 'high'})
        self.assertEqual('completed', self.review().status)


class StageRecordTest(EngineFixture):
    def test_each_stage_is_recorded_in_the_batch_attempt_with_its_artifact_paths(self):
        self.single_reviewer()
        self.review()
        attempt = self.session.load()['batches'][-1]['attempts'][-1]
        self.assertEqual('review_engine', attempt['kind'])
        stage = attempt['stages']['synthesis']
        self.assertEqual('completed', stage['status'])
        self.assertTrue(stage['stdout_log'].endswith('stages/synthesis/events.jsonl'))
        self.assertTrue(Path(stage['output']).is_file())

    def test_a_running_stage_is_visible_in_the_owner_state_while_it_runs(self):
        self.single_reviewer()
        seen = {}
        real = engine_fixture.real_process
        def runner(argv, **kwargs):
            seen['stages'] = self.session.load()['batches'][-1]['attempts'][-1]['stages']
            return real(argv, **kwargs)
        with patch('pr_review.review_workspace.process', side_effect=runner):
            self.review()
        self.assertEqual('running', seen['stages']['synthesis']['status'])


class EngineEntryPointsTest(EngineFixture):
    """Every full-candidate review reaches the shared engine."""

    def test_pr_review_runs_through_the_engine_and_its_pr_adapter(self):
        from types import SimpleNamespace
        from pr_review import pipeline, reviews
        run = reviews.Review.__new__(reviews.Review)
        self.assertIs(reviews.PR_ADAPTER, run.adapter)
        with patch('pr_review.review_engine.synthesis') as synthesis, patch('pr_review.review_engine.independent') as independent, \
                patch('pr_review.review_engine.prepare_slots') as prepare, patch('pr_review.review_engine.execute_stage') as stage:
            pipeline.synthesis(run)
            pipeline.independent(run, 'reviewer-1')
            pipeline.prepare_slots(run, [])
            run.model('synthesis', ['argv'], b'input')
        for spy in (synthesis, independent, prepare, stage):
            spy.assert_called_once()
        self.assertEqual(['synthesis', ['argv'], b'input'], list(stage.call_args.args[1:]))

    def test_issue_acceptance_and_owner_final_verification_run_through_the_engine(self):
        self.single_reviewer()
        with patch('pr_review.review_engine.review', wraps=review_engine.review) as engine:
            self.review()
            result, _ = acceptance.review_owner(self.session, self.state, self.batch, KEYS, self.data)
        self.assertEqual(2, engine.call_count)
        self.assertIsInstance(result, review_engine.Result)


class SingleReviewerTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.single_reviewer()

    def test_no_bubblewrap_is_required_and_synthesis_runs_directly(self):
        real = shutil.which
        def which(name, *args, **kwargs):
            return None if name == 'bwrap' else real(name, *args, **kwargs)
        with patch('pr_review.isolation.shutil.which', side_effect=which):
            result = self.review()
        self.assertEqual('completed', result.status)
        self.assertEqual({'synthesis': 1}, self.counts())
        self.assertTrue((self.workspace() / 'prompts/synthesis-prompt.txt').read_text().startswith('You are the sole reviewer'))

    def test_environment_is_filtered_and_scratch_stays_outside_the_live_worktree(self):
        self.review()
        names = self.env_names.read_text().split()
        self.assertIn('PATH', names)
        self.assertIn('TMPDIR', names)
        self.assertNotIn('EXAMPLE_API_SECRET_TOKEN', names)
        scratch = self.workspace() / 'stages/synthesis/scratch'
        self.assertIn(f'tmp={scratch}', self.marks_text())
        self.assertFalse(str(scratch).startswith(str(self.worktree)))
        self.assertEqual('feature\nlocal edit\n', (self.worktree / 'source.txt').read_text())

    def test_direct_stage_argv_keeps_the_owner_review_restrictions_and_uses_the_checkout(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                self.configure([{'id': 'reviewer-1', 'provider': provider, 'model': 'm', 'effort': 'high'}],
                               {'provider': provider, 'model': 'm', 'effort': 'high'})
                run = review_workspace.CandidateReview.open(self.session, self.state, self.batch, self.adapter(),
                                                            tree=self.batch['checkpoint']['tree'], runtime=providers.Runtime())
                run.adapter.render_synthesis(run)
                argv = acceptance.AcceptanceAdapter.synthesis_command(run.adapter, run)
                checkout = str(run.checkout_path('synthesis'))
                if provider == 'claude':
                    denied = argv[argv.index('--disallowed-tools') + 1:]
                    for tool in providers.DENIED_TOOLS + ['Bash(git add:*)', 'Bash(git update-ref:*)', 'Bash(git config:*)']:
                        self.assertIn(tool, denied)
                    self.assertNotIn('--cd', argv)
                else:
                    self.assertEqual(checkout, argv[argv.index('--cd') + 1])
                    for flag in ('--skip-git-repo-check', '--ignore-rules', 'sandbox_mode=danger-full-access'):
                        self.assertIn(flag, argv)
                self.assertNotEqual(str(self.worktree), checkout)
                self.assertNotIn(str(self.worktree), ' '.join(argv))

    def test_two_slots_work_without_bubblewrap(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        real = shutil.which
        def which(name, *args, **kwargs):
            return None if name == 'bwrap' else real(name, *args, **kwargs)
        with patch('pr_review.isolation.shutil.which', side_effect=which):
            result = self.review()
        self.assertEqual('completed', result.status)
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 1}, self.counts())


class ReuseTest(EngineFixture):
    """The invalidation table, row by row."""

    def baseline_review(self):
        self.review()
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 1}, self.counts())

    def test_previous_bubblewrap_evidence_is_invalidated_and_archived(self):
        self.baseline_review()
        manifest_path = self.workspace() / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        old = manifest['phases']['reviewer-1']
        old['inputs']['isolation'] = 'private-snapshot-bwrap-v1'
        old['fingerprint'] = cache.fingerprint(old['inputs'])
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual({'reviewer-1', 'synthesis'}, self.reran(self.review))
        current = json.loads(manifest_path.read_text())
        self.assertEqual('private-snapshot-container-v1',
                         current['phases']['reviewer-1']['inputs']['isolation'])
        archive = Path(current['review_history'][-1]['archive'])
        historical = json.loads((archive / 'phases.json').read_text())
        self.assertEqual('private-snapshot-bwrap-v1', historical['reviewer-1']['inputs']['isolation'])

    def expect(self, stages, change=None):
        self.baseline_review()
        if change:
            change()
        self.assertEqual(set(stages), self.reran(self.review))

    def test_unchanged_evidence_is_reused_in_full(self):
        self.expect(set())

    def test_resume_after_a_failed_slot_reruns_only_incomplete_stages(self):
        with patch.dict(os.environ, {'FAIL_STAGES': 'reviewer-2'}), self.assertRaises(ApiError):
            self.review()
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1}, self.counts())
        self.assertEqual({'reviewer-2', 'synthesis'}, self.reran(self.review))
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 2, 'synthesis': 1}, self.counts())

    def test_changed_candidate_tree_reruns_everything(self):
        self.baseline_review()
        first = self.workspace()
        self.candidate(content='a different candidate\n')
        self.assertNotEqual(first, self.workspace())
        self.review()
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 1}, self.counts())
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 1},
                         {s: sum(c['stage'] == s for c in read_json(first / 'logs/commands.json')) for s in ('reviewer-1', 'reviewer-2', 'synthesis')})

    def test_changed_baseline_gives_a_separate_commit_ref_and_evidence(self):
        self.baseline_review()
        tree = self.batch['checkpoint']['tree']
        other = self.base.git('commit-tree', tree, '-p', self.batch['baseline'], '-m', 'another baseline', cwd=self.worktree)
        first = owner_git.review_commit(self.session, tree, self.batch['baseline'])
        second = owner_git.review_commit(self.session, tree, other)
        self.assertNotEqual(first['sha'], second['sha'])
        self.assertNotEqual(first['ref'], second['ref'])
        self.batch['baseline'] = other
        self.batch['baseline_ref'] = owner_git.pin_commit(self.session, other)
        self.session.save(self.state)
        self.assertEqual({'reviewer-1', 'reviewer-2', 'synthesis'}, self.reran(self.review))

    def test_changed_specification_or_selected_feedback_reruns_everything(self):
        def change():
            state = self.session.load()
            state['feedback'][0]['comments'][0]['body'] = 'A changed request'
            self.session.save(state)
        self.expect({'reviewer-1', 'reviewer-2', 'synthesis'}, change)

    def test_one_slots_provider_model_or_effort_reruns_that_slot_and_synthesis(self):
        for field, value in (('model', 'another-model'), ('effort', 'low'), ('provider', 'claude')):
            with self.subTest(field=field):
                self.candidate(content=f'edit {field}\n')
                self.configure(self.SLOTS, self.SYNTHESIS)
                self.baseline_review()
                slots = json.loads(json.dumps(self.SLOTS))
                slots[1][field] = value
                self.configure(slots, self.SYNTHESIS)
                self.assertEqual({'reviewer-2', 'synthesis'}, self.reran(self.review))

    def test_synthesis_choice_reruns_synthesis_only(self):
        self.expect({'synthesis'}, lambda: self.configure(self.SLOTS, {**self.SYNTHESIS, 'model': 'other-synthesis'}))

    def test_verification_case_contract_reruns_synthesis_only(self):
        def change():
            self.data['verification_cases'] = [self.case()]
            self.verdict()
        self.expect({'synthesis'}, change)

    def test_implementer_report_reruns_synthesis_only(self):
        self.expect({'synthesis'}, lambda: self.data.update(completed_tasks=[
            {'id': 't1', 'rationale': 'r', 'expected': [], 'implementation': {'summary': 'new claim', 'checks': [], 'outcomes': []}}]))

    def test_earlier_findings_rerun_synthesis_only(self):
        def change():
            state = self.session.load()
            state['batches'][-1]['review'] = {'complete': True, 'summary': 'Rejected', 'blocking': ['Earlier defect'], 'outcomes': []}
            self.session.save(state)
        self.expect({'synthesis'}, change)

    def test_prompt_schema_validator_and_cli_version_rerun_the_stages_that_use_them(self):
        self.baseline_review()
        original_prompt = acceptance.prompt
        def changed_prompt(name):
            return original_prompt(name) + ('\nChanged.' if name == 'synthesis-acceptance.txt' else '')
        with patch('pr_review.acceptance.prompt', side_effect=changed_prompt):
            self.assertEqual({'synthesis'}, self.reran(self.review))
        original_schema = acceptance.output_schema
        def changed_schema(*args, **kwargs):
            return {**original_schema(*args, **kwargs), 'description': 'changed'}
        with patch('pr_review.acceptance.output_schema', side_effect=changed_schema):
            self.assertEqual({'synthesis'}, self.reran(self.review))
        with patch.object(acceptance, 'VALIDATOR_VERSION', 2):
            self.assertEqual({'synthesis'}, self.reran(self.review))
        self.versions['codex'] = 'codex 2.0'
        self.assertEqual({'reviewer-2', 'synthesis'}, self.reran(self.review))
        self.versions['claude'] = 'claude 2.0'
        self.assertEqual({'reviewer-1', 'synthesis'}, self.reran(self.review))
        # The reviewer instructions are part of each slot's identity.
        with patch('pr_review.acceptance.prompt', side_effect=lambda name: original_prompt(name) + (
                '\nChanged.' if name == 'acceptance-reviewer.txt' else '')):
            self.assertEqual({'reviewer-1', 'reviewer-2', 'synthesis'}, self.reran(self.review))

    def test_reused_evidence_is_accepted_only_after_the_live_tree_is_reverified(self):
        self.baseline_review()
        (self.worktree / 'source.txt').write_text('edited after the review\n')
        with patch('pr_review.review_workspace.process') as launched, self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('worktree_changed', caught.exception.code)
        launched.assert_not_called()
        self.assertNoAcceptance()


class IncompleteSynthesisTest(EngineFixture):
    """Decision 8: retained for inspection, never reusable as completed evidence."""

    def setUp(self):
        super().setUp()

    def test_incomplete_then_passed_on_the_unchanged_candidate(self):
        self.verdict('incomplete', summary='The test environment was broken')
        first = self.review()
        self.assertEqual('incomplete', first.status)
        retained = self.workspace() / 'reviews/synthesis-incomplete-1.json'
        self.assertEqual(retained, first.artifact)
        saved = retained.read_bytes()
        phase = self.manifest()['phases']['synthesis']
        self.assertEqual('incomplete', phase['status'])
        self.assertEqual(str(retained), phase['artifact'])
        self.assertFalse((self.workspace() / 'reviews/synthesis.json').exists())
        self.assertNoAcceptance()
        # Resume after the missing dependency returned.
        self.verdict('passed')
        second = self.review()
        self.assertEqual('completed', second.status)
        # Each reviewer ran once in total and synthesis ran twice; the first result is untouched.
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 2}, self.counts())
        self.assertEqual(saved, retained.read_bytes())
        self.assertEqual('completed', self.manifest()['phases']['synthesis']['status'])
        self.assertTrue((self.workspace() / 'reviews/synthesis.json').is_file())

    def test_a_repeated_incomplete_result_stops_again_and_is_not_reused(self):
        self.verdict('incomplete')
        self.assertEqual('incomplete', self.review().status)
        self.assertEqual('incomplete', self.review().status)
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 2}, self.counts())
        reviews = self.workspace() / 'reviews'
        self.assertTrue((reviews / 'synthesis-incomplete-1.json').is_file())
        self.assertTrue((reviews / 'synthesis-incomplete-2.json').is_file())
        self.assertEqual('incomplete', self.manifest()['phases']['synthesis']['status'])

    def test_needs_input_is_retained_and_not_reasked_from_a_stale_result(self):
        self.verdict('needs_input', questions=['Keep archived records searchable?'])
        first = self.review()
        self.assertEqual('needs_input', first.status)
        self.assertTrue((self.workspace() / 'reviews/synthesis-needs-input-1.json').is_file())
        self.verdict('passed')
        self.assertEqual('completed', self.review().status)
        self.assertEqual(2, self.counts()['synthesis'])

    def test_the_engine_refuses_to_reuse_a_damaged_saved_incomplete_result(self):
        self.review()
        reviews = self.workspace() / 'reviews'
        saved = json.loads(self.result_file.read_text())
        # Damage: an incomplete result that was saved as completed evidence.
        (reviews / 'synthesis.json').write_text(json.dumps({**saved, 'assessment': 'incomplete'}))
        record = self.manifest()['phases']['synthesis']
        from pr_review.storage import hash_file
        manifest = self.manifest()
        manifest['phases']['synthesis']['output_hash'] = hash_file(reviews / 'synthesis.json')
        (self.workspace() / 'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual({'synthesis'}, self.reran(self.review))
        self.assertEqual('completed', json.loads((reviews / 'synthesis.json').read_text())['assessment'] and 'completed')
        self.assertEqual('passed', json.loads((reviews / 'synthesis.json').read_text())['assessment'])

    def test_projection_of_an_incomplete_owner_result_pauses_without_a_final_tree(self):
        self.verdict('incomplete', summary='The broken environment hid the evidence')
        result, review = acceptance.review_owner(self.session, self.state, self.batch, KEYS, self.data)
        self.assertEqual('incomplete', result.status)
        self.assertEqual('incomplete', review['assessment'])
        self.assertFalse(review['complete'])
        self.assertEqual(['The broken environment hid the evidence'], review['blocking'])


class NonBlockingObservationTest(EngineFixture):
    """Decision 9: observations stay in engine artifacts; only blocking findings reach legacy proof."""

    def setUp(self):
        super().setUp()
        self.single_reviewer()

    def test_owner_pass_with_observations_projects_an_empty_blocking_list_and_verifies(self):
        self.verdict(findings=[finding('F1', blocking=False, validity='UNVERIFIED', source='new')])
        result, review = acceptance.review_owner(self.session, self.state, self.batch, KEYS, self.data)
        self.assertEqual('completed', result.status)
        self.assertEqual('F1', json.loads(result.artifact.read_text())['findings'][0]['id'])
        self.assertEqual(([], 'passed', True), (review['blocking'], review['assessment'], review['complete']))
        from pr_review import owner_fixes
        review = owner_fixes.validate_result(review, KEYS)
        owner_contracts.bind(review, [], self.batch['checkpoint']['tree'])
        batch = {**self.batch, 'review': review, 'final_tree': self.batch['checkpoint']['tree'], 'tasks': []}
        self.assertTrue(owner_contracts.verified(review, [], batch['final_tree']))
        self.assertTrue(owner_contracts.current(batch))

    def test_mixed_result_projects_only_the_blocking_finding(self):
        self.verdict('changes_requested', findings=[
            finding('F1', blocking=True, source='new', severity='High'), finding('F2', blocking=False, source='new')])
        result, review = acceptance.review_owner(self.session, self.state, self.batch, KEYS, self.data)
        self.assertEqual('changes_requested', review['assessment'])
        self.assertEqual(1, len(review['blocking']))
        self.assertIn('Finding F1', review['blocking'][0])
        self.assertNotIn('Finding F2', ' '.join(review['blocking']))
        self.assertEqual(['F1', 'F2'], [f['id'] for f in json.loads(result.artifact.read_text())['findings']])
        self.assertTrue(owner_fixes_repairable(review))


def owner_fixes_repairable(review):
    from pr_review import owner_fixes
    return owner_fixes.repairable(owner_fixes.validate_result(review, KEYS))


class BridgeTest(EngineFixture):
    def test_same_tree_and_baseline_give_the_same_commit_and_ref(self):
        tree, baseline = self.batch['checkpoint']['tree'], self.batch['baseline']
        first = owner_git.review_commit(self.session, tree, baseline)
        second = owner_git.review_commit(self.session, tree, baseline)
        self.assertEqual(first, second)
        self.assertEqual(self.base.git('rev-parse', first['ref'], cwd=self.worktree), first['sha'])
        self.assertTrue(first['ref'].endswith('/review-candidates/' + first['sha']))
        self.assertEqual(f'{tree}', self.base.git('rev-parse', first['sha'] + '^{tree}', cwd=self.worktree))
        self.assertEqual(baseline, self.base.git('rev-parse', first['sha'] + '^', cwd=self.worktree))
        # Independent of the time and of the user's Git identity.
        with patch.dict(os.environ, {'GIT_AUTHOR_NAME': 'Someone Else', 'GIT_COMMITTER_DATE': '2030-01-01T00:00:00Z',
                                     'GIT_AUTHOR_DATE': '2031-01-01T00:00:00Z'}):
            self.assertEqual(first, owner_git.review_commit(self.session, tree, baseline))

    def test_a_different_baseline_gives_a_different_commit_and_ref(self):
        tree, baseline = self.batch['checkpoint']['tree'], self.batch['baseline']
        other = self.base.git('commit-tree', tree, '-p', baseline, '-m', 'other', cwd=self.worktree)
        self.assertNotEqual(owner_git.review_commit(self.session, tree, baseline)['sha'],
                            owner_git.review_commit(self.session, tree, other)['sha'])

    def test_creation_leaves_head_branch_index_admin_and_worktree_unchanged(self):
        def snapshot():
            return (self.base.git('rev-parse', 'HEAD', cwd=self.worktree), self.base.git('symbolic-ref', 'HEAD', cwd=self.worktree),
                    self.base.git('ls-files', '--stage', cwd=self.worktree), owner_git.admin(self.session, self.state),
                    self.base.git('status', '--porcelain=v1', '--untracked-files=all', cwd=self.worktree),
                    (self.worktree / 'source.txt').read_text())
        before = snapshot()
        owner_git.review_commit(self.session, self.batch['checkpoint']['tree'], self.batch['baseline'])
        self.assertEqual(before, snapshot())

    def test_the_commit_never_joins_the_commit_plan_exact_commits_or_a_push(self):
        commit = owner_git.review_commit(self.session, self.batch['checkpoint']['tree'], self.batch['baseline'])
        head = self.base.git('rev-parse', 'HEAD', cwd=self.worktree)
        self.assertNotIn(commit['sha'], [sha for sha, _, _ in owner_git.commit_records(self.session, self.batch['baseline'], head)])
        self.batch['final_tree'] = self.batch['checkpoint']['tree']
        self.assertEqual([], owner_git.exact_commits(self.session, self.batch, head))
        self.assertEqual('', self.base.git('branch', '--contains', commit['sha'], cwd=self.worktree))
        self.assertEqual(head, self.base.git('rev-parse', 'HEAD', cwd=self.worktree))

    def test_a_real_review_never_produces_an_implementation_commit(self):
        self.single_reviewer()
        head = self.base.git('rev-parse', 'HEAD', cwd=self.worktree)
        self.review()
        self.assertEqual(head, self.base.git('rev-parse', 'HEAD', cwd=self.worktree))
        self.assertEqual(self.batch['baseline'], head)
        self.assertEqual(self.batch['checkpoint']['tree'], self.base.git('rev-parse', self.candidate_sha() + '^{tree}', cwd=self.worktree))


class IntegrityCallbackTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.single_reviewer()

    def test_owner_stages_never_call_repository_verify_but_the_pr_adapter_does(self):
        with patch.object(Repository, 'verify', side_effect=AssertionError('PR-only')) as verify:
            self.assertEqual('completed', self.review().status)
        verify.assert_not_called()
        from pr_review.reviews import PR_ADAPTER
        run = Mock()
        PR_ADAPTER.verify_snapshot(run, 'reviewer-1', 'before')
        PR_ADAPTER.verify_snapshot(run, 'reviewer-1', 'after')
        PR_ADAPTER.verify_snapshot(run, 'reviewer-1', 'reuse')
        self.assertEqual(2, run.repo.verify.call_count)

    def test_a_reviewer_that_edits_the_live_tree_records_an_integrity_failure(self):
        checkpoint = self.batch['checkpoint']
        with patch.dict(os.environ, {'MISBEHAVE': 'live'}), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('integrity_failed', caught.exception.code)
        batch = self.session.load()['batches'][-1]
        self.assertTrue(batch['integrity_failure'])
        self.assertEqual(checkpoint, batch['checkpoint'])              # the checkpoint is unmoved
        self.assertEqual(1, len(batch['integrity_events']))
        self.assertNoAcceptance()
        # Resume requires explicit adoption.
        with self.assertRaises(ApiError) as again:
            self.review()
        self.assertEqual('integrity_failed', again.exception.code)

    def test_a_reviewer_that_changes_git_state_records_an_integrity_failure(self):
        with patch.dict(os.environ, {'MISBEHAVE': 'admin'}), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('integrity_failed', caught.exception.code)
        self.assertTrue(self.session.load()['batches'][-1]['integrity_failure'])
        self.assertNoAcceptance()

    def test_a_live_tree_edit_between_checkpoint_and_review_stops_before_model_work(self):
        (self.worktree / 'source.txt').write_text('edited by a human\n')
        with patch('pr_review.review_workspace.process') as launched, self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('worktree_changed', caught.exception.code)
        launched.assert_not_called()
        self.assertNotIn('integrity_failure', self.session.load()['batches'][-1])


class CheckoutIntegrityTest(EngineFixture):
    """Decision 10: direct stages read a disposable checkout of the bridge commit."""

    def setUp(self):
        super().setUp()
        self.single_reviewer()

    def modified(self, mode, *, hidden=False):
        with patch.dict(os.environ, {'MISBEHAVE': mode}), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('review_checkout_modified', caught.exception.code)
        batch = self.session.load()['batches'][-1]
        self.assertNotIn('integrity_failure', batch)              # the live candidate is untouched
        self.assertNoAcceptance()
        self.assertEqual('feature\nlocal edit\n', (self.worktree / 'source.txt').read_text())
        phase = self.manifest()['phases']['synthesis']
        self.assertEqual(('failed', 'candidate_checkout_modified'), (phase['status'], phase['failure_reason']))
        stage = self.workspace() / 'stages/synthesis'
        self.assertIn('source.txt', (stage / 'checkout-modified.txt').read_text())
        self.assertIn('+changed', (stage / 'checkout-modified.diff').read_text())
        self.assertFalse((self.workspace() / 'reviews/synthesis.json').exists())
        return caught.exception

    def test_stage_and_synthesis_run_in_the_checkout_not_the_live_worktree(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        self.review()
        marks = self.marks_text()
        self.assertIn(f'cwd={self.workspace() / "stages/synthesis/checkout"}', marks)
        self.assertNotIn(f'cwd={self.worktree}', marks)
        self.assertTrue(str(self.workspace()).startswith(str(self.session.path)))
        self.assertFalse(str(self.workspace()).startswith(str(self.worktree)))

    def test_an_edited_tracked_file_discards_the_result_and_resumes_in_a_fresh_checkout(self):
        self.verdict('passed')
        self.modified('edit')
        with patch.dict(os.environ, {'MISBEHAVE': ''}):
            result = self.review()           # no adoption is needed
        self.assertEqual('completed', result.status)
        self.assertEqual(2, self.counts()['synthesis'])
        self.assertTrue((self.workspace() / 'attempts/synthesis-1/checkout-modified.diff').is_file())

    def test_the_same_edit_hidden_with_assume_unchanged_is_still_detected(self):
        self.modified('assume')

    def test_a_local_filter_cannot_hide_an_edit(self):
        self.modified('filter')

    def test_a_moved_head_in_the_checkout_is_detected(self):
        error = self.modified.__func__  # noqa: F841  (documented below)
        with patch.dict(os.environ, {'MISBEHAVE': 'head'}), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('review_checkout_modified', caught.exception.code)
        self.assertNoAcceptance()

    def test_an_untracked_generated_file_does_not_fail_the_check_and_is_retained(self):
        with patch.dict(os.environ, {'MISBEHAVE': 'untracked'}):
            result = self.review()
        self.assertEqual('completed', result.status)
        archive = Path(self.manifest()['phases']['synthesis']['generated_archive'])
        self.assertEqual('generated\n', (archive / 'files/generated.out').read_text())
        self.assertFalse((self.worktree / 'generated.out').exists())

    def test_a_live_tree_edit_during_a_direct_stage_requires_adoption_even_though_the_checkout_is_clean(self):
        with patch.dict(os.environ, {'MISBEHAVE': 'live'}), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual('integrity_failed', caught.exception.code)     # the live violation takes precedence
        self.assertTrue(self.session.load()['batches'][-1]['integrity_failure'])
        self.assertNoAcceptance()

    def test_a_modified_checkout_before_the_stage_stops_without_calling_a_model(self):
        original = review_workspace.CandidateReview.create_checkout
        def tampered(run, stage):
            directory = original(run, stage)
            (run.checkout_path(stage) / 'source.txt').write_text('tampered before the stage\n')
            return directory
        with patch.object(review_workspace.CandidateReview, 'create_checkout', tampered), \
                patch('pr_review.review_workspace.process') as launched:
            launched.side_effect = engine_fixture.real_process
            with self.assertRaises(ApiError) as caught:
                self.review()
        self.assertEqual('review_checkout_modified', caught.exception.code)
        self.assertEqual('', self.marks_text())                          # the scripted model never ran
        self.assertNoAcceptance()

    def test_tracked_content_equals_the_candidate_tree_for_a_clean_checkout(self):
        run = review_workspace.CandidateReview.open(self.session, self.state, self.batch, self.adapter(),
                                                    tree=self.batch['checkpoint']['tree'], runtime=providers.Runtime())
        run.create_checkout('synthesis')
        self.assertEqual([], run.changed_paths('synthesis'))
        (run.checkout_path('synthesis') / 'new.txt').unlink()
        self.assertTrue(any('new.txt' in p for p in run.changed_paths('synthesis')))
        run.close()


class FailureTest(EngineFixture):
    """Failed, empty, invalid, incomplete, integrity, checkout and independence failures keep artifacts."""

    def setUp(self):
        super().setUp()
        self.single_reviewer()

    def stop(self, expected, **environment):
        with patch.dict(os.environ, environment), self.assertRaises(ApiError) as caught:
            self.review()
        self.assertEqual(expected, caught.exception.code)
        self.assertNoAcceptance()
        self.assertTrue(self.workspace().is_dir())
        attempt = self.session.load()['batches'][-1]['attempts'][-1]
        self.assertEqual('review_engine', attempt['kind'])
        self.assertIn(attempt['status'], ('failed', 'interrupted'))
        return attempt

    def test_a_failed_provider_keeps_its_logs(self):
        self.stop('model_failed', MISBEHAVE='fail')
        self.assertTrue((self.workspace() / 'stages/synthesis/stderr.log').exists())
        self.assertEqual('failed', self.manifest()['phases']['synthesis']['status'])

    def test_an_empty_result_is_invalid_after_one_correction(self):
        self.result_file.write_text('')
        self.stop('invalid_artifact')
        self.assertEqual(2, self.counts()['synthesis'])         # one correction attempt, on synthesis only

    def test_an_invalid_result_gets_exactly_one_correction_attempt_on_synthesis_only(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        self.result_file.write_text(json.dumps({'assessment': 'maybe'}))
        self.stop('invalid_result')
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 2}, self.counts())
        self.assertIn('Your previous result was rejected', self.stdin_copy.read_text())

    def test_the_correction_is_not_part_of_the_fingerprint_so_a_corrected_result_is_reusable(self):
        self.result_file.write_text(json.dumps({'assessment': 'maybe'}))
        calls = {'count': 0}
        real = self.command
        def command(run, stage):
            argv = real(run, stage)
            if stage == 'synthesis':
                calls['count'] += 1
                if calls['count'] == 2:      # the correction attempt returns a valid result
                    self.verdict('passed')
            return argv
        with patch.object(review_workspace.CandidateReview, 'command', command):
            self.assertEqual('completed', self.review().status)
        self.assertEqual({'synthesis': []}, {'synthesis': self.reran(self.review) and ['reran']  or []})

    def test_a_result_missing_an_outcome_for_a_selected_item_is_invalid(self):
        self.verdict(outcomes=[{'item': KEYS[0], 'status': 'fixed', 'evidence': 'only one item assessed'}])
        self.stop('invalid_result')

    def test_cancellation_or_a_runtime_limit_during_synthesis_keeps_artifacts_and_sets_no_acceptance(self):
        for error in (Cancelled(signal.SIGTERM), ApiError('resource_limit', 'Runtime expired')):
            with self.subTest(error=error.code), self.failing_process('synthesis', error), \
                    self.assertRaises(ApiError) as caught:
                self.review()
            self.assertIs(error, caught.exception)
            self.assertEqual('interrupted', self.manifest()['phases']['synthesis']['status'])
            self.assertEqual('interrupted', self.session.load()['batches'][-1]['attempts'][-1]['status'])
            self.assertNoAcceptance()
            self.assertTrue((self.workspace() / 'stages/synthesis').is_dir())

    def test_cancellation_during_a_slot_interrupts_it_and_resume_reruns_only_what_lacks_evidence(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        error = Cancelled(signal.SIGTERM)
        with self.failing_process('reviewer-2', error), self.assertRaises(ApiError):
            self.review()
        phases = self.manifest()['phases']
        self.assertEqual(('completed', 'interrupted'), (phases['reviewer-1']['status'], phases['reviewer-2']['status']))
        self.assertNoAcceptance()
        self.assertEqual({'reviewer-2', 'synthesis'}, self.reran(self.review))

    def test_a_runtime_limit_during_a_slot_keeps_its_private_artifacts(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        with self.failing_process('reviewer-1', ApiError('resource_limit', 'Runtime expired')), self.assertRaises(ApiError):
            self.review()
        self.assertEqual('interrupted', self.manifest()['phases']['reviewer-1']['status'])
        self.assertNoAcceptance()

    def test_a_slot_that_edits_its_private_clone_fails_the_isolation_check(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        self.stop('integrity_failed', EDIT_STAGES='reviewer-1')

    def test_a_slot_whose_workspace_cannot_be_prepared_stops_with_independence_failed(self):
        self.configure(self.SLOTS, self.SYNTHESIS)
        with patch('pr_review.isolation.IndependentWorkspace', side_effect=ApiError('independence_failed', 'workspace preparation failed')):
            with self.assertRaises(ApiError) as caught:
                self.review()
        self.assertEqual('independence_failed', caught.exception.code)
        self.assertEqual('failed', self.manifest()['phases']['reviewer-1']['status'])
        self.assertNoAcceptance()

    def test_integrity_and_checkout_failures_never_authorize_a_commit(self):
        for mode in ('live', 'edit'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'MISBEHAVE': mode}), self.assertRaises(ApiError):
                self.candidate(content=f'{mode} candidate\n')
                self.single_reviewer()
                self.review()
            self.assertNoAcceptance()


class ValidatorTest(unittest.TestCase):
    def check(self, value, **kwargs):
        arguments = dict(keys=KEYS, exact_outcomes=False, cases=[], tree='tree', sources={'reviewer-1', 'reviewer-2', 'both', 'new'})
        arguments.update(kwargs)
        return acceptance.validate_acceptance(value, **arguments)

    def value(self, assessment='passed', **fields):
        return {'assessment': assessment, 'summary': 'Summary', 'checks': [], 'findings': [], 'questions': [],
                'outcomes': [{'item': k, 'status': 'fixed', 'evidence': 'verified'} for k in KEYS], 'case_results': [], **fields}

    def rejected(self, value, pattern='', **kwargs):
        with self.assertRaisesRegex(ApiError, pattern) as caught:
            self.check(value, **kwargs)
        self.assertEqual('invalid_result', caught.exception.code)

    def test_passed_needs_one_outcome_per_selected_key_and_no_blockers_clarifications_or_questions(self):
        self.check(self.value())
        self.check(self.value(findings=[finding('F1', blocking=False)]))     # observations are allowed
        self.rejected(self.value(findings=[finding('F1', blocking=True)]))
        self.rejected(self.value(questions=['A question?']))
        self.rejected(self.value(outcomes=self.value()['outcomes'][:1]), 'every selected item')
        outcomes = self.value()['outcomes']
        outcomes[0]['status'] = 'clarify'
        self.rejected(self.value(outcomes=outcomes))
        self.rejected(self.value(outcomes=self.value()['outcomes'] + [{'item': 'unknown', 'status': 'fixed', 'evidence': 'x'}]))

    def test_changes_requested_needs_a_blocking_finding(self):
        self.check(self.value('changes_requested', findings=[finding('F1', blocking=True)]))
        self.rejected(self.value('changes_requested', findings=[finding('F1', blocking=False)]))

    def test_environment_blockers_are_incomplete_not_repairs_or_passes(self):
        blockers = [{'command': 'pytest -m smoke', 'reason': 'Read-only sysctl; needs host networking',
                     'manual_command': 'uv run pytest -m smoke -q'}]
        value = self.value('incomplete', verification_blockers=blockers)
        self.check(value)
        self.assertIsNone(acceptance.project_issue(value))
        self.rejected(self.value(verification_blockers=blockers), 'cannot produce a passed')
        self.rejected(self.value('changes_requested', verification_blockers=blockers), 'blocking finding')
        self.rejected(self.value('incomplete', verification_blockers=[{'command': 'pytest'}]), 'manual command')

    def test_needs_input_and_incomplete(self):
        self.check(self.value('needs_input', questions=['Which behavior?']))
        self.rejected(self.value('needs_input'))
        self.check(self.value('incomplete'))

    def test_findings_are_attributed_and_ids_are_unique(self):
        self.rejected(self.value(findings=[finding('F1', source='someone')]))
        self.rejected(self.value(findings=[finding('F1'), finding('F1')]))
        self.rejected(self.value(findings=[finding('F1', blocking=True, validity='REJECTED')]))

    def test_owner_results_assess_every_item_and_every_verification_case_exactly_once(self):
        case = {'id': 'c1', 'scenario': 's', 'expected': 'e', 'basis': 'b', 'intent': 'change'}
        passing = self.value(case_results=[{'id': 'c1', 'status': 'tested', 'evidence': 'ran'}])
        self.check(passing, exact_outcomes=True, cases=[case])
        self.rejected(self.value(case_results=[]), 'cover each required case', exact_outcomes=True, cases=[case])
        self.rejected(self.value(case_results=[{'id': 'other', 'status': 'tested', 'evidence': 'ran'}]), exact_outcomes=True, cases=[case])
        self.rejected(self.value(case_results=[{'id': 'c1', 'status': 'unavailable', 'evidence': 'no tool'}]),
                      'cannot support a passed', exact_outcomes=True, cases=[case])
        # An incomplete verdict may report an unavailable case; a passed one may not.
        self.check(self.value('incomplete', case_results=[{'id': 'c1', 'status': 'unavailable', 'evidence': 'no tool'}]),
                   exact_outcomes=True, cases=[case])
        self.rejected(self.value('incomplete', outcomes=self.value()['outcomes'][:1]), exact_outcomes=True)
        # Cases are not required for an issue review, and results for unknown cases are rejected.
        self.rejected(self.value(case_results=[{'id': 'c1', 'status': 'tested', 'evidence': 'ran'}]))

    def test_the_acceptance_schema_enumerates_selected_keys_and_attribution(self):
        schema = acceptance.output_schema(KEYS, exact_outcomes=True, sources={'reviewer-1', 'new'})
        item = schema['properties']['outcomes']['items']['properties']['item']
        self.assertEqual(KEYS, item['enum'])
        self.assertEqual((2, 2), (schema['properties']['outcomes']['minItems'], schema['properties']['outcomes']['maxItems']))
        self.assertEqual(['new', 'reviewer-1'], schema['properties']['findings']['items']['properties']['source']['enum'])


class ProjectionTest(unittest.TestCase):
    def value(self, assessment, **fields):
        return {'assessment': assessment, 'summary': 'Summary', 'checks': ['c'], 'questions': [], 'case_results': [],
                'outcomes': [{'item': 'k', 'status': 'fixed', 'evidence': 'verified'}], 'findings': [], **fields}

    def test_issue_projection_follows_the_legacy_proof_shapes(self):
        passed = acceptance.project_issue(self.value('passed', findings=[finding('F1')]))
        self.assertEqual(('ready', [], []), (passed['status'], passed['findings'], passed['questions']))
        rejected = acceptance.project_issue(self.value('changes_requested', findings=[finding('F1', blocking=True), finding('F2')]))
        self.assertEqual('changes_requested', rejected['status'])
        self.assertEqual(1, len(rejected['findings']))
        question = acceptance.project_issue(self.value('needs_input', questions=['Which?']))
        self.assertEqual(('needs_input', ['Which?']), (question['status'], question['questions']))
        self.assertIsNone(acceptance.project_issue(self.value('incomplete')))

    def test_owner_needs_input_becomes_an_incomplete_result_with_a_clarify_outcome(self):
        review = acceptance.project_owner(self.value('needs_input', questions=['Which?']), ['k'])
        self.assertEqual(('incomplete', False, ['Which?']), (review['assessment'], review['complete'], review['blocking']))
        self.assertEqual('clarify', review['outcomes'][0]['status'])
        from pr_review import owner_fixes
        owner_fixes.validate_result(review, ['k'])
        self.assertFalse(owner_fixes.successful(review))

    def test_owner_passed_maps_to_the_unchanged_success_check(self):
        from pr_review import owner_fixes
        review = owner_fixes.validate_result(acceptance.project_owner(self.value('passed', findings=[finding('F1')]), ['k']), ['k'])
        self.assertTrue(owner_fixes.successful(review))
        self.assertEqual([], review['blocking'])


def assert_no_acceptance(self):
    batch = self.session.load()['batches'][-1]
    for field in ('final_tree', 'verified_items', 'verified_fingerprints', 'verified_base', 'harness_review'):
        self.assertNotIn(field, batch)
    self.assertNotEqual('ready', batch['status'])
    self.assertFalse(owner_commit.prepare(self.session, self.session.load(), {'batch': batch['id']})['ready'])


EngineFixture.assertNoAcceptance = assert_no_acceptance


if __name__ == '__main__':
    unittest.main()
