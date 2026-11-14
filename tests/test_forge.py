"""Forge foundation contracts without requiring a second forge transport."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.forge import Project, adapter, restore, check_account, require
from pr_review.common import ApiError


class ForgeTest(unittest.TestCase):
    def test_github_compatibility(self):
        github = adapter()
        project = github.project('Enterprise.Example/Owner/Repo')
        self.assertEqual('enterprise.example/owner/repo', project.storage_key)
        self.assertEqual('7', project.resource_key('change', 7))
        self.assertEqual('https://enterprise.example/owner/repo/pull/7', github.url(project, 7))
        self.assertEqual('refs/pull/7/head', github.head_ref(7))
        self.assertEqual((project, 7), github.target(github.url(project, 7)))

    def test_nested_namespaces_and_storage_isolation(self):
        one = Project('first', 'example.com', 'group/sub/project')
        two = Project('second', 'example.com', 'group/sub/project')
        self.assertNotEqual(one.storage_key, two.storage_key)
        self.assertNotEqual(one.resource_key('issue', 7), one.resource_key('change', 7))
        self.assertEqual('group/sub/project', one.path)

    def test_saved_identity_and_legacy(self):
        state = {'repository': 'github.com/org/repo'}
        original = dict(state)
        project = restore(state)
        self.assertEqual(original, state)
        state['forge_project'] = project.saved()
        self.assertEqual(project, restore(state, 'github'))
        state['forge_project']['path'] = 'other/repo'
        with self.assertRaises(ApiError):
            restore(state)
        with self.assertRaises(ApiError):
            adapter('unknown')

    def test_accounts_are_host_scoped_and_cannot_lose_stable_identity(self):
        account = {'forge': 'github', 'host': 'github.com', 'username': 'User', 'id': '123'}
        check_account(None, account, 'user')
        check_account(account, dict(account), 'user')
        for change in ({'host': 'enterprise.example'}, {'id': None}, {'id': '456'}, {'username': 'other'}):
            with self.subTest(change=change), self.assertRaises(ApiError):
                check_account(account, {**account, **change}, 'user')

    def test_absent_capability_blocks_operation(self):
        with patch('pr_review.forge.GitHub.capabilities', frozenset(('review',))):
            require('github', 'review')
            with self.assertRaises(ApiError):
                require('github', 'publication')

    def test_dispatch_rejects_before_resolving_or_mutating(self):
        from pr_review.application import dispatch
        with patch('pr_review.forge.GitHub.capabilities', frozenset(('review',))), patch('pr_review.application.Session', side_effect=AssertionError('Resolved before capability guard')):
            for command in ('publish', 'prepare', 'owner-publish', 'owner-thread-state', 'issue-run'):
                with self.subTest(command=command), self.assertRaises(ApiError) as caught:
                    dispatch({'command': command, 'session': '/missing'})
                self.assertEqual('unsupported_operation', caught.exception.code)

    def test_unknown_cli_forge_stops_before_settings_or_discovery(self):
        from pr_review import cli, issue_cli, owner_cli
        from contextlib import redirect_stderr
        from io import StringIO
        with patch('pr_review.cli_settings.prepare', side_effect=AssertionError('Prepared settings')), redirect_stderr(StringIO()):
            self.assertEqual(1, cli.main(['7', '--forge', 'unknown']))
            self.assertEqual(1, issue_cli.main(['--issue', '7', '--forge', 'unknown']))
            self.assertEqual(1, owner_cli.main(['7', 'open', '--forge', 'unknown']))

    def test_saved_selection_precedes_default_and_conflicts_are_rejected(self):
        import tempfile
        from pr_review.forge import GitHub, request_forge
        from pr_review.storage import write_json
        class Other(GitHub):
            def project(self, repository):
                host, path = repository.split('/', 1)
                return Project('other', host, path)
        with tempfile.TemporaryDirectory() as directory, patch.dict('pr_review.forge.ADAPTERS', {'other': Other()}):
            project = Project('other', 'example.com', 'group/sub/project')
            write_json(Path(directory) / 'session.json', {'repository': project.repository, 'forge_project': project.saved()})
            self.assertEqual('other', request_forge({'session': directory}))
            with self.assertRaises(ApiError):
                request_forge({'session': directory, 'forge': 'github'})
