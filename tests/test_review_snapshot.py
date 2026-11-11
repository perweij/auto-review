"""Snapshot validation must retain race checks when PR base metadata is stale."""
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.common import ApiError
from pr_review.reviews import Review


class SnapshotTest(unittest.TestCase):
    def test_changed_or_missing_refs_fail_before_worktree_creation(self):
        for mismatch in ('head', 'base', 'branch', 'missing-base'):
            with self.subTest(mismatch=mismatch):
                pr = {'id': 7, 'url': 'https://example.com/7', 'title': 'Title', 'body': '', 'author': 'person',
                      'draft': False, 'state': 'open',
                      'target': {'branch': 'main', 'head': 'old-base', 'project': {'forge': 'github', 'host': 'github.com', 'path': 'org/repo'}},
                      'source': {'head': 'head'}}
                repo = Mock(remote='origin', pr=7, head_ref='local-head', base_ref='local-base')
                latest = copy.deepcopy(pr)
                if mismatch == 'branch':
                    latest['target']['branch'] = 'other'
                repo.forge.change.side_effect = [latest, pr] if mismatch == 'branch' else [latest, latest]

                def git(*args):
                    value = ''
                    if args[0] == 'rev-parse':
                        value = ('wrong-head' if mismatch == 'head' else 'head') if args[1] == 'local-head' else 'base'
                    elif args[0] == 'ls-remote':
                        value = '' if mismatch == 'missing-base' else f'{"new-base" if mismatch == "base" else "base"}\t{args[-1]}\n'
                    return SimpleNamespace(stdout=value)

                repo.git.side_effect = git
                review = SimpleNamespace(manifest={}, repo=repo)
                with patch('pr_review.reviews.say'), self.assertRaises(ApiError) as error:
                    Review.snapshot(review, pr)
                self.assertEqual(error.exception.code, 'snapshot_changed')
                self.assertEqual(repo.forge.change.call_count, 2)
                self.assertFalse(any(call.args[0] == 'worktree' for call in repo.git.call_args_list))
