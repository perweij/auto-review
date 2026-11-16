"""Local target discovery, project isolation, and legacy operation ownership."""
import json
import unittest
from unittest.mock import patch

import test_api
from pr_review.application import dispatch
from pr_review.common import ApiError, atomic_json, state_root
from pr_review.forge import adapter, capabilities
from pr_review.operations import belongs_to, owner
from pr_review.state import Session, discover


class ReviewTargetTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_api.ApiTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.github = adapter().project('github.com/org/repo')
        self.gitlab = adapter('gitlab').project('gitlab.com/group/sub/repo')

    def remote(self, name, url):
        self.fixture.git('remote', 'add', name, url)

    def resolve(self, **fields):
        return dispatch(dict(command='review-target', cwd=str(self.fixture.repo), **fields))

    def snapshot(self):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for folder in (self.fixture.root / 'state', self.fixture.repo)
                for p in folder.rglob('*') if p.is_file()}

    def save_gitlab(self):
        manifest = dict(self.fixture.manifest, repository=self.gitlab.repository, forge_project=self.gitlab.saved())
        path = state_root() / self.gitlab.storage_key / 'pr-7-test'
        atomic_json(path / 'manifest.json', manifest)
        return path

    def test_local_resolution_has_no_external_calls_or_writes(self):
        self.remote('origin', 'git@gitlab.com:Group/Sub/Repo.git')
        before = self.snapshot()
        with patch('pr_review.forge_gitlab.command', side_effect=AssertionError('forge CLI')), \
             patch('pr_review.forge.command', side_effect=AssertionError('forge CLI')), \
             patch('pr_review.models.discover', side_effect=AssertionError('model discovery')):
            result = self.resolve(pr=7)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.gitlab.saved(), result['forge_project'])
        self.assertEqual('https://gitlab.com/group/sub/repo/-/merge_requests/7', result['url'])
        self.assertEqual(capabilities('gitlab'), result['capabilities'])

    def test_multiple_remotes_are_not_overridden_by_saved_sessions(self):
        self.remote('origin', 'https://gitlab.com/group/sub/repo.git')
        self.remote('upstream', 'ssh://git@enterprise.example/team/other.git')
        self.save_gitlab()
        result = self.resolve(pr=7)
        self.assertIsNone(result['forge_project'])
        self.assertEqual(2, len(result['projects']))
        self.assertTrue(next(p for p in result['projects'] if p['repository'] == self.gitlab.repository)['sessions'])
        self.assertEqual(self.gitlab.saved(), self.resolve(pr=7, repository=self.gitlab.repository)['forge_project'])
        self.assertEqual(self.gitlab.saved(), self.resolve(pr=7, forge='gitlab')['forge_project'])

    def test_legacy_saved_identity_is_read_without_migration(self):
        before = self.snapshot()
        self.assertEqual(self.github.saved(), self.resolve(pr=7)['forge_project'])
        self.assertEqual(before, self.snapshot())

    def test_url_variants_are_normalized_and_conflicts_fail(self):
        for path in ('/-/merge_requests/7', '/merge_requests/7?foo=bar#note_8',
                     '/-/merge_requests/7/diffs?diff_id=3#abc', '/-/merge_requests/7/commits',
                     '/-/merge_requests/7/diffs/abc123'):
            result = self.resolve(pr='https://gitlab.com/group/sub/repo' + path)
            self.assertEqual(self.gitlab.saved(), result['forge_project'])
            self.assertEqual(7, result['pr'])
            self.assertEqual(adapter('gitlab').url(self.gitlab, 7), result['url'])
        for fields in (dict(pr=7, repository='other.example/org/repo', forge='gitlab'),
                       dict(pr='https://gitlab.com/group/sub/repo/-/merge_requests/7', forge='github'),
                       dict(pr='https://gitlab.com/group/sub/repo/-/merge_requests/7', repository='gitlab.com/other/repo'),
                       dict(pr=7, repository='gitlab.com/group/sub/repo', forge='github')):
            with self.subTest(fields=fields), self.assertRaises(ApiError):
                self.resolve(**fields)

    def test_discovery_filters_project_and_forge_before_same_number(self):
        self.save_gitlab()
        for forge, project in (('github', self.github), ('gitlab', self.gitlab)):
            for fields in (dict(pr=7, forge=forge), dict(pr=7, repository=project.repository),
                           dict(pr=adapter(forge).url(project, 7))):
                values = discover(dict(cwd=str(self.fixture.repo), **fields))['sessions']
                self.assertEqual(1, len(values))
                self.assertEqual(project.saved(), values[0]['forge_project'])
        self.assertEqual(2, len(discover(dict(cwd=str(self.fixture.repo), pr=7))['sessions']))

    def test_github_browser_excludes_gitlab_remotes_and_sessions(self):
        from pr_review.pulls import list_pulls
        self.remote('origin', 'git@gitlab.com:group/repo.git')
        self.save_gitlab()
        result = list_pulls(dict(cwd=str(self.fixture.repo)))
        self.assertEqual([self.github.repository], result['repositories'])

    def test_operation_ownership_requires_project_or_saved_evidence(self):
        manifest = self.fixture.manifest
        record = dict(common=manifest['git_common_dir'], pr='7', kind='elaborate')
        self.assertFalse(belongs_to(record, manifest, self.fixture.path))
        self.assertTrue(belongs_to(dict(record, **{'forge_project': self.github.saved()}), manifest, self.fixture.path))
        self.assertFalse(belongs_to(dict(record, **{'forge_project': self.gitlab.saved()}), manifest, self.fixture.path))
        self.assertTrue(belongs_to(dict(record, session=str(self.fixture.path)), manifest, self.fixture.path))
        self.assertFalse(belongs_to(dict(record, session=str(self.save_gitlab())), manifest, self.fixture.path))
        self.assertEqual(self.github.saved(), owner(manifest)['forge_project'])

    def test_review_view_omits_foreign_and_unproven_running_operations(self):
        from pr_review.operations import process_identity
        common = self.fixture.manifest['git_common_dir']
        base = dict(process_identity(), common=common, pr='7', kind='elaborate', status='running')
        for identity, fields in (('a', {}), ('b', {'forge_project': self.gitlab.saved()}),
                                 ('c', {'forge_project': self.github.saved()}),
                                 ('d', {'session': str(self.fixture.path)})):
            atomic_json(state_root() / 'operations' / (identity * 32 + '.json'), dict(base, **fields))
        active = self.fixture.session.view(self.fixture.session.load())['operations']
        self.assertEqual({'c' * 32, 'd' * 32}, {op['operation_id'] for op in active})

    def test_launch_records_resolved_project_before_execution(self):
        path = self.save_gitlab()
        session = Session({'session': str(path)})
        def execute(request):
            self.assertEqual('gitlab', request['forge'])
            self.assertEqual(7, request['pr'])
            records = [json.loads(p.read_text()) for p in (state_root() / 'operations').glob('*.json')]
            self.assertEqual(1, len(records))
            self.assertEqual(self.gitlab.saved(), records[0]['forge_project'])
            self.assertEqual('7', records[0]['pr'])
            return session
        with patch('pr_review.reviews.execute', side_effect=execute):
            result = dispatch(dict(command='run', cwd=str(self.fixture.repo), pr=7, repository=self.gitlab.repository))
        self.assertEqual(self.gitlab.saved(), result['sessions'][0]['forge_project'])

    def test_explicit_saved_repository_conflict_is_rejected(self):
        with self.assertRaises(ApiError):
            Session(dict(session=str(self.fixture.path), repository='github.com/other/repo'))

    def test_advertised_gitlab_discovery_and_elaboration_dispatch(self):
        value = self.resolve(pr=7, repository=self.gitlab.repository)['capabilities']
        self.assertIn('discover', value['commands'])
        self.assertIn('elaborate', value['commands'])
        self.assertEqual(['inline', 'review'], value['placements'])
        with patch('pr_review.application.discover', return_value={'sessions': []}) as called:
            self.assertEqual({'sessions': []}, dispatch(dict(command='discover', forge='gitlab')))
            called.assert_called_once()
        with patch('pr_review.application.Session', return_value=object()), \
             patch('pr_review.elaboration.explain', return_value={'body': 'explanation'}) as called:
            self.assertEqual({'body': 'explanation'}, dispatch(dict(command='elaborate', forge='gitlab')))
            called.assert_called_once()
