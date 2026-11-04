import sys
import tempfile
import unittest
from unittest.mock import patch
from pr_review import issue_resources as resources, processes
from pr_review.common import ApiError


class RuntimeTest(unittest.TestCase):
    def test_units_and_invalid_values(self):
        self.assertEqual(7200, resources.duration('2h'))
        self.assertEqual(30, resources.duration('0.5m'))
        for value in ('0s', '-1m', 'nan', '2', True):
            with self.assertRaises(ApiError): resources.duration(value)

    def test_resume_consumes_remaining_allowance_not_a_new_allowance(self):
        state = {'resources': {'version': 1, 'active_seconds': 5, 'max_runtime': 10}}
        with patch('pr_review.issue_resources.time.monotonic', side_effect=[100, 101, 106, 106]):
            with self.assertRaisesRegex(ApiError, 'allowance reached'):
                with resources.execution(state, lambda: None, {}): resources.check()
        self.assertEqual(11, state['resources']['active_seconds'])

    def test_limit_interrupts_owned_subprocess(self):
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaisesRegex(ApiError, 'allowance reached'):
                with resources.execution({}, lambda: None, {'max_runtime': '0.1s'}):
                    processes.run([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=cwd)

    def test_expired_allowance_between_turns_prevents_launch(self):
        clock = [100]
        with patch('pr_review.issue_resources.time.monotonic', side_effect=lambda: clock[0]):
            with resources.execution({}, lambda: None, {'max_runtime': '10s'}):
                clock[0] = 111
                with patch('pr_review.processes.subprocess.Popen') as launch, self.assertRaisesRegex(ApiError, 'allowance reached'):
                    processes.run([sys.executable, '-c', 'pass'], cwd='/tmp')
                launch.assert_not_called()


class QuotaTest(unittest.TestCase):
    def test_missing_telemetry_requires_explicit_runtime_fallback(self):
        record = {'providers': ['claude'], 'min_weekly_quota_remaining': 20}
        with self.assertRaises(ApiError): resources.quota(record)
        record.update(max_runtime=3600)
        record.pop('quota_polled')
        resources.quota(record)
        self.assertEqual(['claude'], record['quota_unavailable'])

    def test_floor_and_consumption_are_independent(self):
        record = {'providers': ['codex'], 'quota_baselines': {}, 'max_weekly_quota_use': 5}
        with patch('pr_review.issue_quota.read_codex', return_value={'weekly': {'used': 10, 'reset': 100}}):
            resources.quota(record)
        record.pop('quota_polled')
        with patch('pr_review.issue_quota.read_codex', return_value={'weekly': {'used': 15, 'reset': 100}}):
            with self.assertRaisesRegex(ApiError, 'threshold'): resources.quota(record)
        self.assertEqual(5, record['quota_consumed']['codex:weekly'])

    def test_reset_does_not_refund_allowance_and_uses_fallback(self):
        record = {'providers': ['codex'], 'quota_baselines': {'codex:weekly': {'used': 40, 'reset': 100}},
                  'quota_consumed': {'codex:weekly': 4}, 'max_weekly_quota_use': 5, 'max_runtime': 100}
        with patch('pr_review.issue_quota.read_codex', return_value={'weekly': {'used': 1, 'reset': 200}}):
            resources.quota(record)
        self.assertEqual(4, record['quota_consumed']['codex:weekly'])
        self.assertTrue(record['quota_unavailable'])


class IssueProviderScopeTest(unittest.TestCase):
    def test_unused_pr_review_provider_is_not_polled(self):
        from pr_review.issue_resources import providers
        state = {'configuration': {'reviewers': ['claude', 'codex'], 'processing_provider': 'codex'},
                 'owner_settings': {'fix': {'provider': 'codex'}, 'review': {'provider': 'codex'}}}
        self.assertEqual(['codex'], providers(state))
        state['owner_settings']['review']['provider'] = 'claude'
        self.assertEqual(['claude', 'codex'], providers(state))


class NamedResourceTest(unittest.TestCase):
    def test_none_disables_each_limit_without_refunding_consumption(self):
        record = {'version': 1, 'active_seconds': 15, 'max_runtime': 30,
                  'min_weekly_quota_remaining': 10, 'max_weekly_quota_use': 2,
                  'quota_consumed': {'codex:weekly': 1}}
        resources.apply_limits(record, dict.fromkeys(('max_runtime', 'min_weekly_quota_remaining', 'max_weekly_quota_use')))
        self.assertEqual({'version': 1, 'active_seconds': 15, 'quota_consumed': {'codex:weekly': 1}}, record)
        with patch('pr_review.issue_resources.time.monotonic', side_effect=[100, 101, 101, 102]):
            with resources.execution({'resources': record}, lambda: None, {}):
                pass
        self.assertEqual(17, record['active_seconds'])
        self.assertEqual({'codex:weekly': 1}, record['quota_consumed'])
        resources.apply_limits(record, {'max_runtime': '1m'})
        self.assertEqual(60, record['max_runtime'])
        self.assertEqual(17, record['active_seconds'])

    def test_named_scope_uses_all_active_roles_and_ignores_interactive_review(self):
        from pr_review import cli_settings
        for provider in ('claude', 'codex'):
            request = {'cli_reviewers': [provider + '-1'], **{'cli_' + r: provider + '-1' for r in ('processing', 'planning', 'fix')}}
            configuration = cli_settings.resolve(request, issue=True)
            state = {'configuration': configuration, 'owner_settings': {'review': {'provider': 'codex' if provider == 'claude' else 'claude'}}}
            self.assertEqual([provider], resources.providers(state))
        configuration = cli_settings.resolve({**request, 'cli_planning': 'claude-2'}, issue=True)
        self.assertEqual(['claude', 'codex'], resources.providers({'configuration': configuration}))

    def test_saved_duration_round_trips_without_exponents_or_precision_loss(self):
        from pr_review import cli_settings
        for seconds in (1080000.0, 1234567.8901234567, 1e-9, 1e100):
            with self.subTest(seconds=seconds):
                text = resources.duration_text(seconds)
                self.assertNotIn('e', text)
                self.assertEqual(seconds, resources.duration(text))
                saved = cli_settings.resolve({}, issue=True)
                resumed = cli_settings.resolve({}, saved, issue=True, resources={'max_runtime': seconds})
                self.assertEqual(seconds, resources.duration(resumed['_named']['max_runtime']))


    def test_stale_named_providers_do_not_override_effective_roles(self):
        from pr_review import cli_settings
        config = cli_settings.resolve({}, issue=True)
        choice = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}
        config.update(reviewer_slots=[{'id': 'reviewer-1', **choice}], processing=choice)
        self.assertEqual(['codex'], resources.providers({'configuration': config}))
        config['planning'] = {'provider': 'claude', 'model': 'opus', 'effort': 'high'}
        config['_named']['model_configs'] = {'codex-1': choice}
        self.assertEqual(['claude', 'codex'], resources.providers({'configuration': config}))
