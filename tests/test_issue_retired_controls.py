"""Retired issue task controls cannot silently affect a harness execution."""
import unittest
from unittest.mock import patch
from pr_review.common import ApiError
from pr_review.issues import dispatch
from pr_review.issue_cli import parser


class RetiredControlsTest(unittest.TestCase):
    def test_cli_no_longer_advertises_task_replanning(self):
        self.assertNotIn('--replan', parser().format_help())

    def test_api_rejects_retired_control_before_io(self):
        with patch('pr_review.issues.resolve') as resolve, self.assertRaisesRegex(ApiError, 'obsolete'):
            dispatch({'command': 'issue-run', 'issue': 12, 'replan': True})
        resolve.assert_not_called()
