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
