"""Report-only GitLab publication and uncertain-delivery contracts."""
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pr_review import report_publication
from pr_review.application import dispatch
from pr_review.common import ApiError
from pr_review.forge import adapter


class GitLabReportsTest(unittest.TestCase):
    def setUp(self):
        self.forge = adapter('gitlab')
        self.adapter = self.forge.report_publication
        self.project = self.forge.project('gitlab.com/group/sub/project')
        self.url = 'https://gitlab.com/group/sub/project/-/merge_requests/7'
        self.repo = SimpleNamespace(cwd=Path('.'), project=self.project, pr=7, url=self.url, forge=self.forge)
        self.account = dict(forge='gitlab', host='gitlab.com', id='123', username='reviewer')
        self.session = SimpleNamespace(manifest=dict(repository=self.project.repository, pr=7,
            forge_project=self.project.saved(), forge_account=self.account, viewer='reviewer'))
        self.raw = dict(id=98, iid=7, target_project_id=45, web_url=self.url, state='merged')
        self.notes, self.actions, self.sent = [], {}, []
        self.save = Mock()
        for target, kwargs in (
            ('pr_review.repository.Repository.from_manifest', dict(return_value=self.repo)),
            ('pr_review.forge_gitlab_reports.command', dict(side_effect=self.command)),
            ('pr_review.report_publication.report_adapter', dict(return_value=self.adapter)),
        ):
            p = patch(target, **kwargs); p.start(); self.addCleanup(p.stop)
        p = patch.object(self.forge, 'get', return_value=self.raw)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(self.forge, 'account', return_value=self.account)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(self.forge, 'pages', side_effect=lambda *a: copy.deepcopy(self.notes))
        self.pages = p.start(); self.addCleanup(p.stop)

    def note(self, body, identity=1):
        return dict(id=identity, body=body, author={'id': 123, 'username': 'reviewer'},
                    system=False, noteable_type='MergeRequest', noteable_id=98, noteable_iid=7, project_id=45)

    def command(self, argv, **kwargs):
        if '--help' in argv:
            return SimpleNamespace(stdout='--hostname --method --input')
        self.assertEqual(['glab', 'api', '--hostname', 'gitlab.com', '--method', 'POST',
                          'projects/45/merge_requests/7/notes', '--input', '-'], argv)
        body = json.loads(kwargs['input_text'])['body']
        self.sent.append(body)
        note = self.note(body, len(self.notes) + 1)
        self.notes.append(note)
        return SimpleNamespace(stdout=json.dumps(note))

    def publish(self, body='report', key='snapshot'):
        return report_publication.publish(self.session, self.actions, key, body, 'reviewer', self.save)

    def test_report_capability_remains_separate_from_owner(self):
        result = dispatch({'command': 'capabilities', 'forge': 'gitlab'})
        self.assertIn('review-publish-report', result['commands'])
        self.assertNotIn('owner-publish-report', result['commands'])
        owner = dispatch({'command': 'capabilities', 'forge': 'gitlab', 'toolkit': 'owner'})
        self.assertIn('owner-publish-report', owner['commands'])
        for op in ('thread-state', 'issue-run'):
            with self.subTest(op=op), self.assertRaises(ApiError) as caught:
                dispatch({'command': op, 'forge': 'gitlab'})
            self.assertEqual('unsupported_operation', caught.exception.code)

    def test_publication_is_bound_idempotent_and_supports_closed_mr(self):
        first = self.publish()
        self.assertEqual(self.url + '#note_1', first['report_url'])
        self.assertEqual(first, self.publish(body='a rerender does not change a frozen operation'))
        self.assertEqual(1, len(self.sent))
        self.publish(key='new-snapshot')
        self.assertEqual(2, len(self.sent))

    def test_accepted_write_with_changed_body_never_repeats(self):
        real = self.command
        def changed(argv, **kwargs):
            result = real(argv, **kwargs)
            if '--help' not in argv:
                self.notes[-1]['body'] += ' edited'
                result.stdout = json.dumps(self.notes[-1])
            return result
        with patch('pr_review.forge_gitlab_reports.command', side_effect=changed):
            with self.assertRaises(ApiError):
                self.publish()
        self.assertIn('receipt', self.actions['snapshot'])
        with self.assertRaises(ApiError):
            self.publish()
        self.assertEqual(1, len(self.sent))
        self.assertEqual('ambiguous', self.actions['snapshot']['status'])

    def test_lost_response_recovers_by_identity_without_resending(self):
        real = self.command
        def lost(argv, **kwargs):
            result = real(argv, **kwargs)
            if '--help' not in argv:
                raise ApiError('command_failed', 'connection lost')
            return result
        with patch('pr_review.forge_gitlab_reports.command', side_effect=lost), self.assertRaises(ApiError):
            self.publish()
        self.assertEqual('complete', self.publish()['report_publication'])
        self.assertEqual(1, len(self.sent))
        self.assertIn('order_by=created_at&sort=asc', self.pages.call_args.args[1])

    def test_unknown_delivery_and_empty_lookup_never_resends(self):
        self.adapter.validate(self.session)
        action = self.adapter.prepare(self.session, 'snapshot', 'report', 'reviewer', 'identity')
        action['status'] = 'ambiguous'
        self.actions['snapshot'] = action
        with self.assertRaises(ApiError) as caught:
            self.publish()
        self.assertEqual('uncertain_report', caught.exception.code)
        self.assertEqual([], self.sent)
        self.assertEqual('ambiguous', action['status'])

    def test_known_failure_before_exec_can_retry(self):
        real = self.command
        def missing(argv, **kwargs):
            if '--help' not in argv:
                raise FileNotFoundError('glab disappeared')
            return real(argv, **kwargs)
        with patch('pr_review.forge_gitlab_reports.command', side_effect=missing), self.assertRaises(ApiError):
            self.publish()
        self.assertEqual('draft', self.actions['snapshot']['status'])
        self.assertEqual('complete', self.publish()['report_publication'])

    def test_definitive_permission_rejection_can_retry_but_server_errors_cannot(self):
        real = self.command
        for status, expected in ((403, 'draft'), (408, 'ambiguous'), (500, 'ambiguous')):
            with self.subTest(status=status):
                self.actions.clear(); self.notes.clear(); self.sent.clear()
                def rejected(argv, **kwargs):
                    if '--help' not in argv:
                        raise ApiError('command_failed', f'glab: Request rejected (HTTP {status})')
                    return real(argv, **kwargs)
                with patch('pr_review.forge_gitlab_reports.command', side_effect=rejected), self.assertRaises(ApiError):
                    self.publish()
                self.assertEqual(expected, self.actions['snapshot']['status'])
                if status == 403:
                    self.assertEqual('complete', self.publish()['report_publication'])
                else:
                    with self.assertRaises(ApiError):
                        self.publish()
                    self.assertFalse(self.sent)

    def test_duplicate_edited_missing_and_malformed_receipts_block(self):
        for kind in ('duplicate', 'edited', 'missing', 'replaced', 'malformed', 'pagination'):
            with self.subTest(kind=kind):
                self.actions.clear(); self.notes.clear(); self.sent.clear()
                self.publish()
                self.actions['snapshot']['status'] = 'ambiguous'
                if kind == 'duplicate':
                    self.notes.append({**self.notes[0], 'id': 2})
                elif kind == 'edited':
                    self.notes[0]['body'] += ' changed'
                elif kind == 'missing':
                    self.notes.clear()
                elif kind == 'replaced':
                    self.notes[0]['id'] = 2
                elif kind == 'malformed':
                    self.notes.append({})
                else:
                    self.pages.side_effect = ApiError('invalid_response', 'page changed')
                with self.assertRaises(ApiError):
                    self.publish()
                self.assertEqual(1, len(self.sent))
                self.pages.side_effect = lambda *a: copy.deepcopy(self.notes)

    def test_destination_actor_and_operation_version_changes_stop_before_write(self):
        for mutation in ({'iid': 8}, {'web_url': 'https://gitlab.com/other/project/-/merge_requests/7'}, {'target_project_id': None}):
            with self.subTest(mutation=mutation), patch.object(self.forge, 'get', return_value={**self.raw, **mutation}), self.assertRaises(ApiError):
                self.publish()
        with patch.object(self.forge, 'account', return_value={**self.account, 'id': '999'}), self.assertRaises(ApiError):
            self.publish()
        self.assertFalse(self.sent)
        self.publish()
        self.actions['snapshot'].update(status='draft', version=99)
        with self.assertRaises(ApiError):
            self.publish()
        self.assertEqual(1, len(self.sent))

    def test_cached_receipt_cannot_be_retargeted_to_another_project(self):
        self.publish()
        with patch.object(self.forge, 'get', return_value={**self.raw, 'target_project_id': 46}), self.assertRaises(ApiError):
            self.publish()
        self.assertEqual(1, len(self.sent))

    def test_body_normalization_and_explicit_quick_action_protection(self):
        self.publish('header\r\n/close\n')
        body = self.sent[0]
        self.assertNotIn('\r', body)
        self.assertIn('/\u200bclose', body)
        action = self.actions['snapshot']
        self.assertTrue(self.adapter.matches(self.session, action, {**self.notes[0], 'body': body + '\r\n  '}))
        for mutation in ({'project_id': 46}, {'noteable_iid': 8}, {'author': {'id': 999, 'username': 'reviewer'}}):
            self.assertFalse(self.adapter.matches(self.session, action, {**self.notes[0], **mutation}))

    def test_publish_specific_help_requirement_stops_before_send(self):
        with patch('pr_review.forge_gitlab_reports.command', return_value=SimpleNamespace(stdout='--hostname --method')), self.assertRaises(ApiError):
            self.publish()
        self.assertFalse(self.sent)

    def test_api_publishes_explicit_historical_generation_without_redirecting(self):
        import shutil
        from test_api import ApiTest
        from pr_review import review_reports, session_trace
        from pr_review.common import atomic_json, read_json
        from pr_review.state import Session
        fixture = ApiTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        manifest = fixture.manifest
        manifest.update(repository=self.project.repository, forge_project=self.project.saved(),
                        forge_account=self.account, url=self.url)
        review_reports.new_generation(manifest)
        atomic_json(fixture.path / 'manifest.json', manifest)
        session_trace.initialize(review_reports.location(fixture.path, manifest),
                                 review_reports.binding(fixture.path, manifest))
        historical = Session({'session': str(fixture.path)})
        current = Path(str(fixture.path) + '-history') / 'next'
        shutil.copytree(fixture.path, current)
        new_manifest = read_json(current / 'manifest.json')
        new_manifest['session_directory'] = str(fixture.path)
        review_reports.new_generation(new_manifest)
        atomic_json(current / 'manifest.json', new_manifest)
        (fixture.path / '.current-review').symlink_to(current)
        current_id = Session({'session': str(fixture.path)}).id
        result = dispatch({'command': 'review-publish-report', 'session': str(fixture.path),
                           'generation': historical.id})
        self.assertEqual(historical.id, result['generation'])
        self.assertIn(historical.id, self.sent[0])
        self.assertNotIn(current_id, self.sent[0])
        self.assertNotIn('execution_report', read_json(current / 'manifest.json'))
