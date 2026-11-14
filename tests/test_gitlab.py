"""Read-only GitLab adapter contracts."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.forge import adapter, restore
from pr_review.common import ApiError
from pr_review.application import dispatch
from pr_review.providers import environment, DENIED_TOOLS


class GitLabTest(unittest.TestCase):
    def setUp(self):
        self.forge = adapter('gitlab')
        self.repo = SimpleNamespace(cwd='.', pr=7, project=self.forge.project('gitlab.com/group/Sub/repo'), url='https://gitlab.com/group/Sub/repo/-/merge_requests/7')
        self.raw = dict(iid=7, web_url=self.repo.url, title='Title', description=None, author={'username': 'author'}, state='opened', draft=False,
                        source_project_id=2, target_project_id=1, source_branch='feature', target_branch='main', sha='a'*40,
                        diff_refs=dict(head_sha='a'*40, base_sha='b'*40, start_sha='c'*40))

    def test_identity_and_scoped_resume(self):
        project, number = self.forge.target(self.repo.url)
        self.assertEqual(7, number)
        self.assertEqual('group/sub/repo', project.path)
        self.assertEqual(project, restore(dict(repository=project.repository, forge_project=project.saved())))
        for bad in ('https://other.example/a/b/-/merge_requests/7', '-1', '0'):
            with self.assertRaises(ApiError):
                self.forge.target(bad)
        with self.assertRaises(ApiError):
            self.forge.project('other.example/a/b')

    def test_fork_metadata_and_diff_version(self):
        with patch.object(self.forge, 'get', side_effect=[self.raw, {'path_with_namespace': 'fork/sub/repo'}]):
            change = self.forge.change(self.repo)
        self.assertEqual('fork/sub/repo', change['source']['project']['path'])
        self.assertEqual(self.raw['diff_refs'], change['diff_refs'])
        self.assertTrue(change['cross_project'])
        for mutation in ({'sha': 'd'*40}, {'diff_refs': {}}, {'iid': 8}):
            with patch.object(self.forge, 'get', return_value={**self.raw, **mutation}), self.assertRaises(ApiError):
                self.forge.change(self.repo)

    def test_pages_and_repeated_page(self):
        first = [{'id': i} for i in range(100)]
        with patch.object(self.forge, 'get', side_effect=[first, [{'id': 100}]]) as get:
            self.assertEqual(101, len(self.forge.pages(self.repo, 'collection')))
            self.assertIn('page=2', get.call_args.args[1])
        with patch.object(self.forge, 'get', side_effect=[first, first]), self.assertRaises(ApiError):
            self.forge.pages(self.repo, 'collection')
        with patch.object(self.forge, 'get', return_value=[{'id': []}]), self.assertRaises(ApiError):
            self.forge.pages(self.repo, 'collection')

    def test_transport_is_explicit_get(self):
        with patch('pr_review.forge_gitlab.command', return_value=SimpleNamespace(stdout='[]')) as command:
            self.forge.get(self.repo, 'projects/group%2FSub%2Frepo')
            self.assertEqual(['glab', 'api', '--hostname', 'gitlab.com', '--method', 'GET', 'projects/group%2FSub%2Frepo'], command.call_args.args[0])
        with patch('pr_review.forge_gitlab.command', return_value=SimpleNamespace(stdout='oops')), self.assertRaises(ApiError):
            self.forge.get(self.repo, 'user')

    def test_discussion_notes_opaque_identity_and_diagnostics(self):
        notes = [dict(id=1, author={'username': 'a'}, body='body', created_at='now', updated_at='now', system=False, resolvable=True, resolved=False,
                      position=dict(new_path='file', new_line=3, head_sha='a'*40, start_sha='c'*40, base_sha='b'*40))]
        with patch.object(self.forge, 'change', return_value={'source': {'head': 'a'*40}}), patch.object(self.forge, 'pages', side_effect=[[{'id': 'opaque', 'individual_note': False, 'notes': notes}]]):
            value, diagnostics = self.forge.discussion(self.repo)
        self.assertEqual('thread:opaque', value['items'][0]['key'])
        self.assertFalse(value['items'][0]['is_outdated'])
        self.assertEqual(notes, diagnostics['discussions'][0]['notes'])

    def test_unsupported_operations_fail_before_session_or_transport(self):
        with patch('pr_review.application.Session', side_effect=AssertionError('Session opened')):
            for op in ('prepare', 'publish', 'resolve', 'automatic-review', 'owner-fix', 'owner-read', 'issue-run', 'issue-eligibility', 'thread-state'):
                with self.subTest(op=op), self.assertRaises(ApiError) as caught:
                    dispatch(dict(command=op, forge='gitlab'))
                self.assertEqual('unsupported_operation', caught.exception.code)

    def test_optional_ci_and_cancellation(self):
        change = {'source': {'head': 'a'*40}}
        with patch.object(self.forge, 'discussion', return_value=({'items': []}, {})), patch.object(self.forge, 'pages', side_effect=ApiError('failed', 'Unavailable')):
            context, _ = self.forge.review_context(self.repo, change, [], [])
            self.assertFalse(context['ci']['available'])
            self.assertTrue(context['warnings'])
        with patch.object(self.forge, 'discussion', side_effect=ApiError('cancelled', 'Stopped')), self.assertRaises(ApiError):
            self.forge.review_context(self.repo, change, [], [])

    def test_model_credentials_and_command_restriction(self):
        with patch.dict('os.environ', {'GITLAB_TOKEN': 'secret', 'GLAB_TOKEN': 'secret'}):
            env, removed = environment('codex')
            self.assertNotIn('GITLAB_TOKEN', env)
            self.assertNotIn('GLAB_TOKEN', env)
        self.assertIn('Bash(glab:*)', DENIED_TOOLS)

    def test_capabilities_exclude_writes(self):
        value = dispatch({'command': 'capabilities', 'forge': 'gitlab'})
        self.assertIn('run', value['commands'])
        self.assertNotIn('publish', value['commands'])
        self.assertNotIn('owner', value['features'])

    def test_malformed_thread_is_required_context_failure(self):
        for thread in ({'id': 'opaque', 'individual_note': False, 'notes': []},
                       {'id': 'opaque', 'individual_note': 'false', 'notes': []}):
            with patch.object(self.forge, 'change', return_value={'source': {'head': 'a'*40}}), patch.object(self.forge, 'pages', return_value=[thread]), self.assertRaises(ApiError) as caught:
                self.forge.discussion(self.repo)
            self.assertEqual('incomplete_discussion', caught.exception.code)

    def test_deleted_and_inaccessible_source_keep_target_ref_reviewable(self):
        deleted = {**self.raw, 'source_project_id': None}
        with patch.object(self.forge, 'get', return_value=deleted):
            change = self.forge.change(self.repo)
            self.assertIsNone(change['source']['project'])
            self.assertTrue(change['warnings'])
        with patch.object(self.forge, 'get', side_effect=[self.raw, ApiError('command_failed', 'HTTP 404')]):
            self.assertIsNone(self.forge.change(self.repo)['source']['project'])
        with patch.object(self.forge, 'get', side_effect=[self.raw, ApiError('cancelled', 'Stopped')]), self.assertRaises(ApiError) as caught:
            self.forge.change(self.repo)
        self.assertEqual('cancelled', caught.exception.code)

    def test_host_lookalike_is_rejected_and_case_is_normalized(self):
        with self.assertRaises(ApiError):
            self.forge.target('https://gitlab-com/group/project/-/merge_requests/7')
        self.assertEqual(self.forge.project('group/Sub/repo'), self.forge.project('GROUP/sub/REPO'))

    def test_unknown_ci_status_does_not_discard_known_checks(self):
        pipelines = [dict(id=1, sha='a'*40, status='success', web_url='https://gitlab.com/p/1'),
                     dict(id=2, sha='a'*40, status='skipped', web_url='https://gitlab.com/p/2')]
        with patch.object(self.forge, 'discussion', return_value=({'items': []}, {})), patch.object(self.forge, 'pages', return_value=pipelines):
            value, _ = self.forge.review_context(self.repo, {'source': {'head': 'a'*40}}, [], [])
        self.assertEqual('passed', value['ci']['checks'][0]['state'])
        self.assertFalse(value['ci']['complete'])
        self.assertIn('skipped', value['ci']['coverage'])

    def test_removed_line_anchor_uses_old_path(self):
        note = dict(id=1, author={'username': 'a'}, body='body', created_at='now', updated_at='now', system=False, resolvable=True, resolved=True,
                    position=dict(new_path='renamed', old_path='original', old_line=4, head_sha='b'*40, start_sha='c'*40, base_sha='d'*40))
        with patch.object(self.forge, 'change', return_value={'source': {'head': 'a'*40}}), patch.object(self.forge, 'pages', return_value=[{'id': 'opaque', 'individual_note': False, 'notes': [note]}]):
            value, _ = self.forge.discussion(self.repo)
        thread = value['items'][0]
        self.assertEqual(('original', 4, 'old'), (thread['path'], thread['line'], thread['side']))
        self.assertTrue(thread['is_outdated'])
        self.assertTrue(thread['is_resolved'])
