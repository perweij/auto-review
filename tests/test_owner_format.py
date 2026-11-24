"""Offline checks for Owner's locked Ruff CI gate."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pr_review import owner_format
from pr_review.common import ApiError
from pr_review.processes import Result


class OwnerFormatTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        workflows = self.root / '.github/workflows'
        workflows.mkdir(parents=True)
        self.workflow = workflows / 'ci.yml'
        self.workflow.write_text('jobs:\n  test:\n    steps:\n      - run: uv run --no-sync ruff format --check .\n')
        (self.root / 'pyproject.toml').touch()
        (self.root / 'uv.lock').write_text('[[package]]\nname = "ruff"\nversion = "0.16.6"\n')
        (self.root / '.pre-commit-config.yaml').write_text('rev: v0.16.4\n')
        (self.root / 'new file.py').write_text('a=1\n')
        (self.root / 'alias.py').symlink_to(self.root / 'new file.py')
        self.state = {'worktree': str(self.root)}
        self.session = SimpleNamespace(repo=Mock())
        self.session.repo.git.return_value.stdout = 'new file.py\0alias.py\0removed.py\0'
        self.batch = {}
        check = patch.object(owner_format, 'check_tree', side_effect=lambda session, tree, invoke: invoke(['--check', '.']))
        check.start()
        self.addCleanup(check.stop)

    def gate(self, final=True):
        return owner_format.gate(self.session, self.state, self.batch, 'base', {'tree': 'raw'}, final=final)

    def test_only_root_steps_enable_formatting(self):
        workflow = self.workflow.read_text()
        self.assertFalse(owner_format.root_ci_step(workflow + '        working-directory: nested\n'))
        self.assertFalse(owner_format.root_ci_step('defaults:\n  run:\n    shell: bash\n    working-directory: sub\n' + workflow))
        self.assertTrue(owner_format.root_ci_step(workflow + '      - run: echo done\n        working-directory: nested\n'))

    @patch.object(owner_format.owner_git, 'checkpoint')
    @patch.object(owner_format, 'run')
    def test_final_repair_and_recheck_bind_formatted_tree(self, run, checkpoint):
        checkpoint.side_effect = [{'tree': t} for t in ('raw', 'formatted', 'formatted')]
        run.side_effect = [Result(1, 'Would reformat: new file.py', ''), Result(0, '', ''), Result(0, '', '')]
        self.assertEqual(self.gate()['tree'], 'formatted')
        self.assertEqual(self.batch['ruff_check'], {'tree': 'formatted', 'version': '0.16.6'})
        args = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all('ruff==0.16.6' in cmd for cmd in args))
        self.assertEqual(args[1][-3:], ['--force-exclude', '--', 'new file.py'])
        self.assertEqual(args[0][-2:], args[2][-2:])

    @patch.object(owner_format.owner_git, 'checkpoint', return_value={'tree': 'raw'})
    @patch.object(owner_format, 'run', return_value=Result(1, 'Would reformat: untouched.py', ''))
    def test_untouched_failure_surfaces_without_retry_loop(self, run, checkpoint):
        self.session.repo.git.return_value.stdout = ''
        with self.assertRaisesRegex(ApiError, 'untouched.py'):
            self.gate()
        self.assertTrue(all(call.args[0][-2:] == ['--check', '.'] for call in run.call_args_list))
        self.assertNotIn('ruff_check', self.batch)

    @patch.object(owner_format.owner_git, 'checkpoint', return_value={'tree': 'raw'})
    @patch.object(owner_format, 'run', return_value=Result(1, 'Would reformat: new file.py', ''))
    def test_verify_only_never_formats(self, run, checkpoint):
        self.batch['verify_only'] = True
        with self.assertRaisesRegex(ApiError, 'new file.py'):
            self.gate()
        self.assertEqual(run.call_count, 1)

    @patch.object(owner_format, 'run', side_effect=FileNotFoundError('uv unavailable'))
    def test_missing_tool_and_unmatched_ci(self, run):
        with self.assertRaisesRegex(ApiError, 'uv unavailable'):
            self.gate()
        self.workflow.unlink()
        self.assertEqual(self.gate()['tree'], 'raw')
        self.assertEqual(run.call_count, 1)
