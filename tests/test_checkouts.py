"""Checkout selection is local, read-only, and generation-specific."""
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.checkouts import lookup, announce
from pr_review.events import event_sink
from pr_review.storage import write_json


class CheckoutTest(unittest.TestCase):
    def test_historical_mapping_wins_over_stale_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, archive, retained = [root / name for name in ('current', 'history', 'retained')]
            for path in (current, archive, retained):
                path.mkdir()
            write_json(current / 'manifest.json', {'snapshot_history': [
                {'archive': str(archive), 'worktree': str(retained)}]})
            write_json(archive / 'manifest.json', {})
            session = SimpleNamespace(path=archive, current=current, manifest={'worktree': str(current)},
                                      paths=lambda: [current, archive])
            with patch('pr_review.state.Session', return_value=session):
                result = lookup({'toolkit': 'review', 'generation': 'old'})
                self.assertEqual(str(retained), result['path'])
                self.assertTrue(result['available'])
                retained.rmdir()
                self.assertFalse(lookup({'toolkit': 'review'})['available'])
                self.assertFalse(retained.exists())

    def test_owner_missing_and_live_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            for path, available in ((None, False), (directory, True), (directory + '/missing', False)):
                session = SimpleNamespace(load=lambda: {'worktree': path})
                with patch('pr_review.owner_state.OwnerSession', return_value=session):
                    self.assertEqual(available, lookup({'toolkit': 'owner'})['available'])

    def test_announcement_is_absolute_and_skips_missing_paths(self):
        events = []
        with tempfile.TemporaryDirectory() as directory, event_sink(lambda kind, **fields: events.append((kind, fields))):
            announce(directory, 'artifacts', 'review', 'generation')
            announce(directory + '/missing', 'artifacts', 'review')
        self.assertEqual(1, len(events))
        kind, fields = events[0]
        self.assertEqual('progress', kind)
        self.assertEqual('Checkout: ' + str(Path(directory).resolve()), fields['message'])
        self.assertEqual('generation', fields['generation'])

    def test_review_cli_deduplicates_checkout_announcements(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from pr_review import cli
        with tempfile.TemporaryDirectory() as directory:
            def execute(*args, **kwargs):
                announce(directory, '/artifacts', 'review', 'g')
                announce(directory, '/artifacts', 'review', 'g')
                return SimpleNamespace(root='/artifacts')
            for resume in ([], ['--resume', '/artifacts']):
                output = StringIO()
                with patch('pr_review.cli_settings.prepare'), patch('pr_review.reviews.execute', side_effect=execute), redirect_stdout(output):
                    self.assertEqual(0, cli.main(['7', '--no-publish', *resume]))
                self.assertEqual(1, output.getvalue().count('Checkout: '))

    def test_fix_cli_keeps_checkout_progress_out_of_json(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        import json
        from pr_review import issue_cli
        with tempfile.TemporaryDirectory() as directory:
            def dispatch(request):
                announce(directory, '/owner', 'owner')
                announce(directory, '/owner', 'owner')
                return {'outcome': 'complete', 'exit_code': 0}
            output, progress = StringIO(), StringIO()
            with patch('pr_review.cli_settings.prepare'), patch('pr_review.issue_cli.dispatch', side_effect=dispatch), redirect_stdout(output), redirect_stderr(progress):
                self.assertEqual(0, issue_cli.main(['--issue', '7', '--json']))
            self.assertEqual('complete', json.loads(output.getvalue())['outcome'])
            self.assertEqual(1, progress.getvalue().count('Checkout: '))

    def test_unmapped_history_never_opens_new_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SimpleNamespace(path=root / 'old', current=root, paths=lambda: [],
                                      manifest={'worktree': directory, 'snapshot': {'head': 'old-head'}})
            with patch('pr_review.state.Session', return_value=session), patch('pr_review.checkouts.git', return_value=SimpleNamespace(returncode=0, stdout='new-head\n')):
                self.assertFalse(lookup({'toolkit': 'review'})['available'])
