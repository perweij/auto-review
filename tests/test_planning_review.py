import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pr_review import planning_review as planning, cli_settings
from pr_review.common import ApiError, git


class PlanningReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        git(['init', '-q'], cwd=self.repo)
        git(['config', 'user.email', 'test@example.com'], cwd=self.repo)
        git(['config', 'user.name', 'Test'], cwd=self.repo)
        (self.repo/'source.txt').write_text('source\n')
        git(['add', '.'], cwd=self.repo)
        git(['commit', '-qm', 'base'], cwd=self.repo)
        self.home = self.root/'home'
        self.home.mkdir()
        env = patch.dict(os.environ, {'HOME': str(self.home), 'CODEX_HOME': str(self.home/'.codex'), 'CLAUDE_CONFIG_DIR': str(self.home/'.claude'), 'XDG_CONFIG_HOME': str(self.home/'.config')})
        env.start(); self.addCleanup(env.stop)
        self.config = cli_settings.resolve({}, issue=True)
        self.calls = []
        self.fail_stage = None
        self.assessment = {'decision': 'no_change', 'summary': 'Existing behavior is correct', 'plan': '', 'acceptance': [], 'questions': [], 'inspected': [{'path': 'source.txt', 'finding': 'Meets requirements'}], 'decisions': []}
        p = patch('pr_review.planning_review.run', side_effect=self.run_model)
        p.start(); self.addCleanup(p.stop)
        p = patch('pr_review.planning_review.response', side_effect=lambda choice, result, directory, **kwargs: copy.deepcopy(result.value))
        p.start(); self.addCleanup(p.stop)

    def run_model(self, args, **kwargs):
        stage = kwargs['stage'].removeprefix('planning-')
        self.calls.append((stage, args, kwargs))
        if self.fail_stage == stage:
            raise ApiError('model_failed', 'Stopped')
        if stage == 'draft':
            kwargs['on_line'](json.dumps({'type': 'thread.started', 'thread_id': 'planner-session'}))
        value = {'summary': 'Checked', 'points': []} if stage == 'review' else copy.deepcopy(self.assessment)
        if stage == 'synthesis':
            value['review_dispositions'] = []
        return SimpleNamespace(value=value)

    def assess(self, **kwargs):
        return planning.assess({'issue': 'Verify source'}, self.config, self.root/'assessments', self.repo, **kwargs)

    def test_three_stages_resume_planner_and_reuse_completed_results(self):
        result = self.assess()
        self.assertEqual('no_change', result['decision'])
        self.assertEqual(['draft', 'review', 'synthesis'], [c[0] for c in self.calls])
        self.assertIn('resume', self.calls[-1][1])
        self.assertIn('planner-session', self.calls[-1][1])
        self.assertNotEqual(self.calls[0][2]['env']['HOME'], self.calls[1][2]['env']['HOME'])
        self.assertEqual(['planning-draft', 'planning-review', 'planning-synthesis'],
                         [c[2]['usage_call']['role'] for c in self.calls])
        self.config['planning']['effort'] = 'medium'
        self.assess()
        self.assertEqual(3, len(self.calls))

    def test_partial_failure_and_regeneration_keep_upstream_work(self):
        self.fail_stage = 'review'
        with self.assertRaises(ApiError): self.assess()
        self.fail_stage = None
        self.assess()
        self.assertEqual(['draft', 'review', 'review', 'synthesis'], [c[0] for c in self.calls])
        self.assess(rerun={'id': 'rerun', 'stages': ['synthesis']})
        self.assertEqual('synthesis', self.calls[-1][0])
        count = len(self.calls)
        self.assess(rerun={'id': 'rerun', 'stages': ['synthesis']})
        self.assertEqual(count, len(self.calls))

    def test_dispositions_require_every_point(self):
        with self.assertRaises(ApiError):
            planning.validate('synthesis', {**self.assessment, 'review_dispositions': []}, self.repo, [{'id': 'P1'}])

    def test_old_complete_configuration_stays_single(self):
        old = cli_settings.defaults(issue=True)
        old.pop('planning_review'); old.pop('planning_synthesis')
        old['model_configs'].pop('claude-2')
        normalized = cli_settings.validate(old, issue=True)
        self.assertIsNone(normalized['planning_review'])
        self.assertEqual('claude', self.config['planning_review']['provider'])

    def test_source_change_runs_all_stages_again(self):
        self.assess()
        (self.repo/'source.txt').write_text('new source\n')
        git(['add', '.'], cwd=self.repo); git(['commit', '-qm', 'new'], cwd=self.repo)
        self.assess()
        self.assertEqual(6, len(self.calls))

    def test_review_policy_change_retains_draft(self):
        self.assess()
        with patch.dict(planning.INSTRUCTIONS, {'review': planning.INSTRUCTIONS['review'] + ' Check compatibility.'}):
            self.assess()
        self.assertEqual(['draft', 'review', 'synthesis', 'review', 'synthesis'], [c[0] for c in self.calls])

    def test_partial_settings_change_preserves_original_attribution(self):
        self.fail_stage = 'review'
        with self.assertRaises(ApiError): self.assess()
        self.config['planning']['effort'] = 'medium'
        self.config['planning_review']['effort'] = 'medium'
        self.fail_stage = None
        result = self.assess()
        self.assertEqual('high', result['planning_choices']['draft']['effort'])
        self.assertEqual('medium', result['planning_choices']['review']['effort'])
        self.assertEqual('medium', result['planning_choices']['synthesis']['effort'])

    def test_explicit_settings_and_synthesis_following_planner(self):
        config = cli_settings.resolve({'cli_planning': 'codex-7', 'named_codex-7-effort': 'medium'}, issue=True)
        self.assertEqual('medium', config['planning']['effort'])
        self.assertIsNone(config['planning_synthesis'])
        single = cli_settings.resolve({'cli_planning_review': 'none'}, config, issue=True)
        self.assertIsNone(single['planning_review'])
        resumed = cli_settings.resolve({}, single, issue=True)
        self.assertIsNone(resumed['planning_review'])

    def test_provider_handoff_starts_fresh_synthesis(self):
        self.config['planning_synthesis'] = {'provider': 'claude', 'model': 'opus', 'effort': 'high'}
        self.assess()
        self.assertIn('--session-id', self.calls[-1][1])
        self.assertNotIn('--resume', self.calls[-1][1])

    def test_invalid_review_gets_only_one_correction_across_resumes(self):
        real = self.run_model
        def invalid(args, **kwargs):
            result = real(args, **kwargs)
            if kwargs['stage'] == 'planning-review':
                result.value = {'summary': 'Invalid', 'points': [{'id': 42}]}
            return result
        with patch('pr_review.planning_review.run', side_effect=invalid):
            with self.assertRaises(ApiError): self.assess()
            count = len(self.calls)
            with self.assertRaises(ApiError): self.assess()
            self.assertEqual(count, len(self.calls))
        self.assess(rerun={'id': 'repair', 'stages': ['review']})
        self.assertEqual(['draft', 'review', 'review', 'review', 'synthesis'], [c[0] for c in self.calls])

    def test_missing_review_provider_fails_before_draft(self):
        def missing(name, path):
            if name == 'claude':
                raise ApiError('independence_failed', 'Missing Claude')
            return '/usr/bin/' + name
        with patch('pr_review.planning_review.isolation._executable', side_effect=missing):
            with self.assertRaisesRegex(ApiError, 'Missing Claude'): self.assess()
        self.assertEqual([], self.calls)

    def test_damaged_draft_invalidates_downstream_even_with_identical_output(self):
        result = self.assess()
        journal = Path(result['planning_artifacts'])/'stages.json'
        state = json.loads(journal.read_text())
        (Path(state['stages']['draft']['directory'])/'validated.json').write_text('{}')
        self.assess()
        self.assertEqual(['draft', 'review', 'synthesis', 'draft', 'review', 'synthesis'], [c[0] for c in self.calls])

    def test_incomplete_configuration_is_not_migrated(self):
        value = cli_settings.defaults(issue=True)
        value.pop('planning_synthesis')
        with self.assertRaisesRegex(ApiError, 'Missing fields'): cli_settings.validate(value, issue=True)

    def test_product_and_technical_unresolved_dispositions(self):
        value = {**self.assessment, 'review_dispositions': [{'id': 'R1', 'status': 'unresolved', 'reason': 'Needs a decision'}]}
        for kind in ('product', 'technical'):
            with self.assertRaises(ApiError):
                planning.validate('synthesis', value, self.repo, [{'id': 'R1', 'kind': kind}])
        value['review_dispositions'][0]['id'] = 1
        with self.assertRaises(ApiError): planning.validate('synthesis', value, self.repo, [{'id': 'R1'}])

    def test_validated_result_recovers_without_repeating_model_call(self):
        result = self.assess()
        journal = Path(result['planning_artifacts'])/'stages.json'
        state = json.loads(journal.read_text())
        state['stages']['synthesis']['status'] = 'pending'
        journal.write_text(json.dumps(state))
        self.config['planning']['effort'] = 'medium'
        recovered = self.assess()
        self.assertEqual(3, len(self.calls))
        self.assertEqual('high', recovered['planning_choices']['synthesis']['effort'])

    def test_same_provider_critic_starts_fresh_without_planner_transcript(self):
        self.config['planning_review'] = copy.deepcopy(self.config['planning'])
        self.assess()
        stage, args, call = self.calls[1]
        self.assertEqual('review', stage)
        self.assertIn('--ephemeral', args)
        self.assertNotIn('resume', args)
        self.assertNotIn('planner-session', call['input_bytes'].decode())
        self.assertNotEqual(self.calls[0][2]['env']['CODEX_HOME'], call['env']['CODEX_HOME'])

    def test_malformed_successful_response_is_correctable_but_provider_failure_is_not(self):
        from pr_review.structured import response
        result = SimpleNamespace(returncode=0, events=[{'type': 'result', 'subtype': 'success', 'result': 'not JSON'}])
        with self.assertRaises(ApiError) as caught:
            response({'provider': 'claude'}, result, self.root, invalid_code='invalid_result')
        self.assertEqual('invalid_result', caught.exception.code)
        result.returncode = 1
        with self.assertRaises(ApiError) as caught:
            response({'provider': 'claude'}, result, self.root, invalid_code='invalid_result')
        self.assertEqual('model_failed', caught.exception.code)
