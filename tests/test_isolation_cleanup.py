"""Temporary reviewer cleanup must not replace the stage's result."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.common import ApiError
from pr_review.isolation import IndependentWorkspace


class CleanupTest(unittest.TestCase):
    def test_permission_failure_reports_retained_path(self):
        workspace = IndependentWorkspace.__new__(IndependentWorkspace)
        workspace.temp = SimpleNamespace(name='/tmp/private-review', cleanup=Mock(side_effect=PermissionError(1, 'Operation not permitted', 17)))
        with patch('pr_review.isolation.emit') as emit:
            workspace.close()
        self.assertIn('/tmp/private-review', emit.call_args.kwargs['message'])
        self.assertIn('manual cleanup', emit.call_args.kwargs['message'])

    def test_cleanup_does_not_mask_initialization_failure(self):
        original = ApiError('independence_failed', 'Original preparation failure')
        def initialize(workspace, *args):
            workspace.temp = SimpleNamespace(name='/tmp/private-review', cleanup=Mock(side_effect=PermissionError(1, 'denied')))
            raise original
        with patch.object(IndependentWorkspace, '_initialize', initialize), patch('pr_review.isolation.emit'):
            with self.assertRaises(ApiError) as caught:
                IndependentWorkspace(None, 'reviewer-1', 'claude')
        self.assertIs(original, caught.exception)
