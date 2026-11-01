"""Interrupted review children must leave a resumable artifact inventory."""
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

from pr_review.common import ApiError
from pr_review.processes import Cancelled
from pr_review.reviews import Review
from pr_review import providers


class ReviewInterruptionTest(unittest.TestCase):
    def test_child_artifacts_are_saved_on_cancellation_and_resource_exhaustion(self):
        cases = [(error, stage) for error in (Cancelled(signal.SIGTERM), ApiError('resource_limit', 'Time exhausted'))
                 for stage in ('synthesis', 'reviewer-1')]
        for error, stage in cases:
            with self.subTest(error=error.code, stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                worktree = root / 'worktree'
                worktree.mkdir()
                subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
                (worktree / '.gitignore').write_text('__pycache__/\n')
                subprocess.run(['git', '-C', str(worktree), 'add', '.gitignore'], check=True)
                run = object.__new__(Review)
                run.path = root / 'artifacts'
                (run.path / 'logs').mkdir(parents=True)
                run.worktree = worktree
                run.settings = {'processing_provider': 'codex', 'codex_model': 'test', 'synthesis_tests': 'focused'}
                if stage == 'reviewer-1':
                    run.settings['reviewer_slots'] = [{'id': stage, 'provider': 'codex', 'model': 'test', 'effort': 'high'}]
                sandbox = Mock()
                sandbox.wrap.side_effect = lambda argv, env: (list(argv), env)
                archive = root / 'private-artifacts'
                sandbox.preserve_generated.return_value = (archive, ['private.pyc'])
                run.runtime = providers.Runtime()
                run.manifest = {'phases': {}, 'snapshot': {'head': 'head'}, 'generated_files': []}
                run.repo = SimpleNamespace(common=root, verify=Mock(), git=Mock(return_value=SimpleNamespace(stdout='A  .gitignore\n')))
                run.save = lambda: (root / 'saved.json').write_text(json.dumps(run.manifest))

                def process(argv, **kwargs):
                    if argv[0] == 'git':
                        return SimpleNamespace(returncode=0, stderr='', stdout=f'{worktree}\n{root}\nhead\n')
                    (worktree / '__pycache__').mkdir()
                    (worktree / '__pycache__/test.pyc').write_bytes(b'cache')
                    (worktree / 'generated.txt').write_text('retained')
                    raise error

                with patch('pr_review.reviews.cache.inputs', return_value={}), patch('pr_review.reviews.process', side_effect=process), \
                        patch('pr_review.isolation.IndependentSandbox', return_value=sandbox):
                    with self.assertRaises(ApiError) as caught:
                        run.model(stage, ['fake-provider'])
                self.assertIs(error, caught.exception)
                saved = json.loads((root / 'saved.json').read_text())
                expected = ['!! __pycache__/test.pyc', '?? generated.txt']
                if stage == 'reviewer-1':
                    expected.append(f'{stage}: private.pyc (saved in {archive})')
                    sandbox.preserve_generated.assert_called_once()
                    sandbox.close.assert_called_once()
                    self.assertEqual(str(archive), saved['phases'][stage]['generated_archive'])
                else:
                    sandbox.close.assert_not_called()
                self.assertEqual(expected, saved['generated_files'])
                self.assertEqual('interrupted', saved['phases'][stage]['status'])
                self.assertTrue((run.path / f'logs/{stage}.files-after.txt').exists())
                self.assertNotIn('A  .gitignore', saved['generated_files'])

    def test_provider_children_disable_python_bytecode(self):
        for provider in ('claude', 'codex'):
            self.assertEqual('1', providers.environment(provider)[0]['PYTHONDONTWRITEBYTECODE'])
