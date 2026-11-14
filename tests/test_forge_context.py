"""Read contracts use application facts, not GitHub payloads."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import cache, discussion
from pr_review.common import ApiError
from pr_review.forge_context import FORMAT, validate_change, validate_discussion
from pr_review.reviews import Review


def change():
    project = {'forge': 'test', 'host': 'example.com', 'path': 'group/sub/project'}
    return {'id': 'opaque-change', 'url': 'https://example.com/change/one', 'title': 'Title',
            'body': 'Body', 'author': 'person', 'state': 'open', 'draft': False,
            'source': {'project': project, 'branch': 'feature', 'head': 'a' * 40},
            'target': {'project': project, 'branch': 'main', 'head': 'b' * 40}, 'permissions': {'reply': None}}


class ReadContractTest(unittest.TestCase):
    def test_opaque_ids_and_unknown_permissions(self):
        self.assertEqual('opaque-change', validate_change(change())['id'])
        value = {'items': [{'key': 'thread:opaque', 'kind': 'thread', 'can_reply': None, 'root_id': 'note:opaque', 'url': 'https://example.com/thread', 'is_resolved': False, 'is_outdated': False,
                           'comments': [{'id': 'note:opaque', 'author': 'person', 'body': 'Text',
                                         'url': 'https://example.com/note', 'created_at': 'now', 'updated_at': 'now'}]}]}
        self.assertIs(value, validate_discussion(value))
        self.assertIsNone(value['items'][0]['can_reply'])
        repo = SimpleNamespace(forge=SimpleNamespace(discussion=lambda _: (value, {})))
        self.assertEqual((value, {}), discussion.fetch(repo))
        value['items'].append(value['items'][0])
        with self.assertRaises(ApiError):
            discussion.fetch(repo)

    def test_new_context_uses_normalized_adapter_and_separate_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Review.__new__(Review)
            run.path = Path(temporary)
            (run.path / 'context').mkdir()
            run.worktree = run.path
            (run.path / 'context/pr.json').write_text('legacy context')
            run.manifest = {'phases': {'context': {'status': 'completed', 'output_hash': 'invalidated'}}, 'warnings': [], 'snapshot': {'merge_base': 'b' * 40, 'head': 'a' * 40}}
            run.save = lambda: None
            payload = {'change': change(), 'warnings': [], 'timeline': [], 'reviews': [], 'inline': None,
                       'ci': {'checks': [], 'coverage': 'unavailable', 'available': False, 'complete': False}}
            run.repo = SimpleNamespace(forge=SimpleNamespace(review_context=lambda *_: (payload, {'native': 'opaque'})),
                                       git=lambda *_args, **_kwargs: SimpleNamespace(stdout=''))
            run.context(change(), [], [])
            self.assertEqual(FORMAT, run.manifest['context_format'])
            self.assertEqual(payload, json.loads((run.path / 'context/review-context.json').read_text()))
            self.assertTrue((run.path / 'diagnostics/forge-context.json').exists())
            self.assertFalse((run.path / 'context/pr.json').exists())

    def test_completed_legacy_context_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Review.__new__(Review)
            run.path = Path(temporary)
            context = run.path / 'context'
            context.mkdir()
            (context / 'pr.json').write_text('{"legacy":"unchanged"}')
            expected = cache.context_hash(context)
            run.manifest = {'phases': {'context': {'status': 'completed', 'output_hash': expected}}}
            run.context(None, None, None)  # No repository or adapter is needed for reuse.
            self.assertEqual(expected, cache.context_hash(context))
            self.assertNotIn('context_format', run.manifest)

    def test_cancellation_is_not_optional_context(self):
        from pr_review.forge_github import review_context
        repo = SimpleNamespace(pr=1, name='group/project', pages=lambda *_: (_ for _ in ()).throw(ApiError('cancelled', 'Stopped')))
        with self.assertRaises(ApiError) as caught:
            review_context(repo, change(), [], [])
        self.assertEqual('cancelled', caught.exception.code)

    def test_github_fork_translation_preserves_both_projects(self):
        from pr_review.forge import adapter
        github = adapter()
        value = {'number': 7, 'url': 'https://github.com/base/project/pull/7', 'title': 'Title',
                 'body': 'Body', 'author': {'login': 'person'}, 'state': 'OPEN', 'isDraft': False,
                 'headRefOid': 'a' * 40, 'baseRefOid': 'b' * 40, 'headRefName': 'feature',
                 'baseRefName': 'main', 'isCrossRepository': True,
                 'headRepository': {'name': 'project'}, 'headRepositoryOwner': {'login': 'fork'}}
        result = github.change_from_legacy(value, github.project('base/project'))
        self.assertEqual('fork/project', result['source']['project']['path'])
        self.assertEqual('base/project', result['target']['project']['path'])
        self.assertIsNone(result['permissions']['maintainer_can_modify'])

    def test_optional_ci_failure_is_explicit_not_empty_success(self):
        from pr_review.forge_github import review_context
        def pages(endpoint, kind):
            if 'actions/runs' in endpoint:
                raise ApiError('unavailable', 'CI unavailable')
            return []
        repo = SimpleNamespace(pr=1, name='group/project', pages=pages)
        context, diagnostics = review_context(repo, change(), [], [])
        self.assertFalse(context['ci']['complete'])
        self.assertTrue(context['inline_available'])
        self.assertNotIn('discussion_complete', context)
        self.assertTrue(context['ci']['available'])
        self.assertIsNone(diagnostics['runs'])
        self.assertTrue(context['warnings'])

    def test_initial_required_discussion_is_validated(self):
        from pr_review.forge_github import review_context
        repo = SimpleNamespace(pr=1, name='group/project', pages=lambda *_: [])
        context, _ = review_context(repo, change(), [{'id': 1, 'body': 'Missing author and URL'}], [])
        self.assertIsNone(context['timeline'])
        self.assertTrue(context['warnings'])

    def test_cancelled_status_refresh_clears_previous_approval(self):
        from pr_review.storage import write_json
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'discussion/review-status.json'
            write_json(path, {'available': True, 'decision': 'APPROVED'})
            repo = SimpleNamespace(forge=SimpleNamespace(review_status=lambda _: (_ for _ in ()).throw(ApiError('cancelled', 'Stopped'))))
            with self.assertRaises(ApiError):
                discussion.collect_status(repo, temporary)
            self.assertEqual({'available': False}, json.loads(path.read_text()))

    def test_shared_boundary_rejects_incomplete_threads_and_unknown_ci(self):
        from pr_review.forge_context import read_context, read_change
        with self.assertRaises(ApiError):
            read_change(SimpleNamespace(forge=SimpleNamespace(change=lambda _: {'id': 'incomplete'})))
        thread = {'key': 'thread:one', 'kind': 'thread', 'comments': []}
        with self.assertRaises(ApiError):
            validate_discussion({'items': [thread]})
        context = {'change': change(), 'warnings': [], 'timeline': [], 'reviews': [], 'inline': [],
                   'ci': {'available': True, 'complete': True, 'coverage': 'test', 'checks': [{'state': 'success'}]}}
        repo = SimpleNamespace(forge=SimpleNamespace(review_context=lambda *_: (context, {})))
        with self.assertRaises(ApiError):
            read_context(repo, change(), [], [])

    def test_nested_namespace_remote_parsing_uses_selected_adapter(self):
        from pr_review.forge import GitHub, Project
        from pr_review.pulls import repository_name
        class Nested(GitHub):
            def project(self, repository):
                host, path = repository.split('/', 1)
                return Project('nested', host, path)
        with patch.dict('pr_review.forge.ADAPTERS', {'nested': Nested()}):
            for remote in ('https://example.com/group/Sub/project.git', 'git@example.com:group/Sub/project.git',
                           'ssh://git@example.com/group/Sub/project.git'):
                self.assertEqual('example.com/group/Sub/project', repository_name(remote, 'nested'))
