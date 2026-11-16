"""GitLab native positions, frozen drafts and multi-operation recovery."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pr_review import publication
from pr_review.common import ApiError, atomic_json
from pr_review.context import GitContext
from pr_review.forge import adapter
from pr_review.forge_gitlab_publication import position, render_body
from pr_review.state import Session
import test_api


class GitLabPublicationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_api.ApiTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.forge = adapter('gitlab')
        self.project = self.forge.project('gitlab.com/group/sub/repo')
        self.account = dict(forge='gitlab', host='gitlab.com', id='123', username='reviewer')
        self.url = 'https://gitlab.com/group/sub/repo/-/merge_requests/7'
        self.fixture.manifest.update(repository=self.project.repository, forge_project=self.project.saved(),
                                     forge_account=self.account, url=self.url)
        atomic_json(self.fixture.path / 'manifest.json', self.fixture.manifest)
        self.session = Session({'session': str(self.fixture.path)})
        self.repo = SimpleNamespace(cwd=self.fixture.repo, project=self.project, pr=7, url=self.url, forge=self.forge)
        self.raw = dict(id=70, iid=7, target_project_id=1, web_url=self.url, sha=self.session.git.head, state='opened')
        self.version = dict(id=4, state='collected', head_commit_sha=self.session.git.head,
                            base_commit_sha=self.session.git.base, start_commit_sha=self.session.git.base)
        self.notes, self.discussions, self.posts = [], [], []
        self.state = self.session.load()
        for i, c in enumerate(self.state['comments']):
            c.update(status='staged', body=f'Finding {i}', placement={'kind': 'inline', 'path': 'source.txt', 'side': 'RIGHT', 'line': i+1})
        for target, kwargs in (
            ('pr_review.repository.Repository.from_manifest', dict(return_value=self.repo)),
            ('pr_review.forge_gitlab_publication.command', dict(side_effect=self.command)),
            ('pr_review.publication.repository_operation', dict()),
            ('pr_review.publication.refresh', dict(return_value=[])),
            ('pr_review.workflow.status_fingerprint', dict(return_value='stable')),
        ):
            p = patch(target, **kwargs); p.start(); self.addCleanup(p.stop)
        for method, kwargs in (
            ('account', dict(return_value=self.account)), ('get', dict(side_effect=self.get)),
            ('pages', dict(side_effect=self.pages)),
        ):
            p = patch.object(self.forge, method, **kwargs); p.start(); self.addCleanup(p.stop)

    def get(self, repo, endpoint):
        return copy.deepcopy(self.version if '/versions/' in endpoint else self.raw)

    def pages(self, repo, endpoint):
        return copy.deepcopy([self.version] if '/versions' in endpoint else self.discussions if '/discussions' in endpoint else self.notes)

    def command(self, argv, **kwargs):
        if '--help' in argv:
            return SimpleNamespace(stdout='--hostname --method --input')
        self.assertEqual(['api', '--hostname', 'gitlab.com', '--method', 'POST'], argv[1:6])
        payload = json.loads(kwargs['input_text'])
        self.posts.append((argv[6], payload))
        note = dict(id=len(self.notes)+1, body=payload['body'], author={'id': 123, 'username': 'reviewer'},
                    system=False, project_id=1, noteable_id=70, noteable_iid=None, noteable_type='MergeRequest')
        inline = 'position' in payload
        if inline:
            note['position'] = copy.deepcopy(payload['position'])
        self.notes.append(note)
        discussion = dict(id='opaque-' + str(note['id']), individual_note=not inline, notes=[note])
        self.discussions.append(discussion)
        return SimpleNamespace(stdout=json.dumps(discussion if inline else note))

    def prepare(self):
        return publication.prepare(self.session, self.state)

    def publish(self):
        preview = self.prepare()
        self.assertTrue(preview['ready'], preview)
        return publication.publish(self.session, self.state, {'confirmation': preview['confirmation']})

    def test_native_batch_preserves_markdown_and_has_receipts_for_each_comment(self):
        self.state['comments'][0].update(placement={'kind': 'review'}, body='**Finding** @reviewer\r\n/close\n```sh\n/close\n```\n')
        before = self.state['comments'][0]['body']
        preview = self.prepare()
        self.assertIn('/\u200bclose', preview['review']['comments'][0]['publication_body'])
        self.assertEqual(before, self.state['comments'][0]['body'])
        result = publication.publish(self.session, self.state, {'confirmation': preview['confirmation']})
        self.assertTrue(result['complete'])
        self.assertTrue(self.posts[0][0].endswith('/notes'))
        self.assertTrue(self.posts[1][0].endswith('/discussions'))
        self.assertTrue(all(c['status'] == 'published' and c['receipt']['html_url'].startswith(self.url + '#note_') for c in self.state['comments']))

    def test_prepared_preview_projects_frozen_payloads_and_only_omits_exact_suffix(self):
        self.state['comments'][0]['body'] = '/close\n\n<!-- pr-review action=literal -->\nKeep this marker.'
        before = copy.deepcopy(self.state['comments'])
        prepared = self.prepare()
        for row, op in zip(prepared['preview']['operations'], self.state['prepared']['actions'][0]['operations']):
            self.assertEqual(row['body'] + '\n\n' + op['marker'], op['payload']['body'])
            self.assertEqual(row['position'], op['payload'].get('position'))
        self.assertIn('/\u200bclose', prepared['preview']['operations'][0]['body'])
        self.assertIn('<!-- pr-review action=literal -->', prepared['preview']['operations'][0]['body'])
        self.assertEqual(before, self.state['comments'])

    def test_retry_preview_uses_reused_payload_and_excludes_confirmed_operations(self):
        prepared = self.prepare()
        previous = copy.deepcopy(self.state['prepared']['actions'][0])
        previous['status'] = 'failed'
        previous['operations'][0].update(status='published', receipt_verified=True, delivery='confirmed')
        previous['operations'][1].update(status='failed', delivery='not_sent')
        self.state['batches'].append(dict(actions=[previous]))
        # Recovery has been tested separately; freeze its outcome here so this
        # assertion specifically checks projection after failed-action reuse.
        with patch.object(publication, 'recover'):
            retry = self.prepare()
        rows = retry['preview']['operations']
        self.assertEqual(1, len(rows))
        self.assertEqual(previous['operations'][1]['id'], rows[0]['operation_id'])
        self.assertEqual(prepared['preview']['operations'][1], rows[0])
        self.assertEqual({'inline': 1, 'replies': 0, 'overall': 0}, retry['counts'])

    def test_terminal_previews_the_normalized_body_before_confirmation(self):
        from pr_review import terminal
        self.state['comments'][0]['body'] = '**Finding**\n/close'
        preview = self.prepare()
        output = io.StringIO()
        def response(request):
            return preview if request['command'] == 'prepare' else preview['review']
        with patch.object(terminal, 'call', side_effect=response) as calls, \
                patch('builtins.input', side_effect=['p', 'no', 'q']), redirect_stdout(output):
            terminal.terminal(str(self.session.path))
        self.assertIn('**Finding**\n/\u200bclose', output.getvalue())
        self.assertNotIn('**Finding**\n/close', output.getvalue())
        self.assertNotIn('publish', [c.args[0]['command'] for c in calls.call_args_list])

    def test_partial_accepted_write_recovers_and_never_reposts_confirmed_operation(self):
        real = self.command
        def lost(argv, **kwargs):
            value = real(argv, **kwargs)
            if '--help' not in argv and len(self.posts) == 2:
                raise ApiError('command_failed', 'Response lost')
            return value
        with patch('pr_review.forge_gitlab_publication.command', side_effect=lost):
            self.assertFalse(self.publish()['complete'])
        publication.recover(self.session, self.state)
        self.assertEqual(2, len(self.posts))
        self.assertTrue(all(c['status'] == 'published' for c in self.state['comments']))
        self.assertFalse(self.prepare()['ready'])

    def test_partial_rejection_resumes_only_the_unsent_remainder(self):
        real = self.command
        def denied(argv, **kwargs):
            if '--help' not in argv and len(self.posts) == 1:
                raise ApiError('command_failed', 'glab: Forbidden (HTTP 403)')
            return real(argv, **kwargs)
        with patch('pr_review.forge_gitlab_publication.command', side_effect=denied):
            self.assertFalse(self.publish()['complete'])
        action = self.state['batches'][0]['actions'][0]
        self.assertEqual(['published', 'failed'], [op['status'] for op in action['operations']])
        self.assertTrue(self.publish()['complete'])
        self.assertEqual(2, len(self.posts))
        self.assertEqual(1, len(self.state['batches'][0]['actions']))

    def test_unknown_delivery_empty_lookup_does_not_become_failed_retry(self):
        real = self.command
        def lost(argv, **kwargs):
            if '--help' not in argv:
                raise ApiError('command_failed', 'Transport lost')
            return real(argv, **kwargs)
        with patch('pr_review.forge_gitlab_publication.command', side_effect=lost):
            self.assertFalse(self.publish()['complete'])
        with self.assertRaises(ApiError) as caught:
            self.prepare()
        self.assertEqual('uncertain_publication', caught.exception.code)
        self.assertFalse(self.posts)

    def test_retry_discards_old_non_delivery_proof_before_another_attempt(self):
        real = self.command
        attempts = []
        def failed(argv, **kwargs):
            if '--help' in argv:
                return real(argv, **kwargs)
            operation = next(op for batch in self.state['batches'] for action in batch['actions']
                             for op in action['operations'] if op['status'] == 'submitting')
            self.assertEqual('unknown', operation['delivery'])
            attempts.append(1)
            raise ApiError('command_failed', 'Forbidden (HTTP 403)' if len(attempts) == 1 else 'Response lost')
        with patch('pr_review.forge_gitlab_publication.command', side_effect=failed):
            self.assertFalse(self.publish()['complete'])
            self.assertFalse(self.publish()['complete'])
        with self.assertRaises(ApiError):
            self.prepare()
        self.assertEqual(2, len(attempts))
        self.assertFalse(self.posts)

    def test_edited_duplicate_missing_and_malformed_receipts_never_authorize_retry(self):
        for kind in ('edited', 'duplicate', 'missing', 'malformed'):
            with self.subTest(kind=kind):
                self.state['batches'] = []; self.notes.clear(); self.discussions.clear(); self.posts.clear()
                for c in self.state['comments']:
                    c.update(status='staged', receipt=None)
                self.assertTrue(self.publish()['complete'])
                action = self.state['batches'][-1]['actions'][0]
                op = action['operations'][0]
                action['status'] = op['status'] = 'ambiguous'
                if kind == 'edited':
                    self.discussions[0]['notes'][0]['body'] += ' changed'
                elif kind == 'duplicate':
                    self.discussions.append(copy.deepcopy(self.discussions[0]))
                elif kind == 'missing':
                    self.discussions.clear()
                else:
                    self.discussions.append({})
                with self.assertRaises(ApiError):
                    publication.recover(self.session, self.state)
                self.assertEqual(2, len(self.posts))

    def test_unsupported_intents_and_bad_versions_fail_before_posting(self):
        for change in ('APPROVE', 'REQUEST_CHANGES', 'reply', 'suggestion', 'version', 'head'):
            with self.subTest(change=change):
                state = copy.deepcopy(self.state)
                if change in ('APPROVE', 'REQUEST_CHANGES'):
                    state['verdict'] = change
                elif change == 'reply':
                    state['comments'][0]['placement'] = {'kind': 'reply'}
                elif change == 'suggestion':
                    state['comments'][0]['suggestion'] = {'replacement': 'new'}
                elif change == 'version':
                    self.version['state'] = 'collecting'
                else:
                    self.raw['sha'] = 'a' * 40
                with self.assertRaises(ApiError):
                    self.forge.publication.prepare(self.session, state)
                self.version['state'] = 'collected'; self.raw['sha'] = self.session.git.head
        self.assertFalse(self.posts)

    def test_own_timeline_and_inline_roots_are_filtered_but_other_replies_remain(self):
        from pr_review.discussion import external
        self.state['comments'][0]['placement'] = {'kind': 'review'}
        self.assertTrue(self.publish()['complete'])
        overview, inline = self.notes
        discussion = {'items': [dict(key='comment:1', kind='comment', comments=[overview]),
                                dict(key='thread:opaque', kind='thread', comments=[inline, {'id': 99, 'body': 'New external reply'}])]}
        result = external(discussion, self.state['batches'][0]['actions'], self.forge.publication)
        self.assertEqual(1, len(result['items']))
        self.assertEqual([99], [c['id'] for c in result['items'][0]['comments']])

    def test_changed_account_destination_head_or_version_blocks_a_confirmed_preview(self):
        baseline = copy.deepcopy(self.state)
        for failure in ('account', 'destination', 'head', 'version'):
            with self.subTest(failure=failure):
                self.state = copy.deepcopy(baseline)
                preview = self.prepare()
                account, raw, version = copy.deepcopy(self.account), copy.deepcopy(self.raw), copy.deepcopy(self.version)
                if failure == 'account':
                    self.account['id'] = '999'
                elif failure == 'destination':
                    self.raw['target_project_id'] = 999
                elif failure == 'head':
                    self.raw['sha'] = 'a' * 40
                else:
                    self.version['state'] = 'collecting'
                try:
                    result = publication.publish(self.session, self.state, {'confirmation': preview['confirmation']})
                finally:
                    self.account.clear(); self.account.update(account)
                    self.raw.clear(); self.raw.update(raw)
                    self.version.clear(); self.version.update(version)
                self.assertFalse(result['complete'])
                self.assertFalse(self.posts)

    def test_single_lines_context_pairs_and_ranges_have_native_cursor_codes(self):
        refs = dict(base_sha=self.session.git.base, start_sha=self.session.git.base, head_sha=self.session.git.head)
        for side, line, expected in (('LEFT', 2, {'old_line': 2}), ('RIGHT', 2, {'new_line': 2}), ('RIGHT', 3, {'old_line': 3, 'new_line': 3})):
            value = position(self.session, dict(path='source.txt', side=side, line=line), refs)
            self.assertEqual(expected, {k: value[k] for k in ('old_line', 'new_line') if k in value})
        value = position(self.session, dict(path='source.txt', side='RIGHT', start_line=1, line=3), refs)
        sha = hashlib.sha1(b'source.txt').hexdigest()
        self.assertEqual(sha + '_1_1', value['line_range']['start']['line_code'])
        self.assertEqual(sha + '_3_3', value['line_range']['end']['line_code'])

    def test_new_deleted_and_renamed_paths_come_from_git_snapshot(self):
        fixture = self.fixture
        fixture.git('mv', 'source.txt', 'renamed.txt')
        (fixture.repo / 'added.txt').write_text('one\ntwo\n')
        fixture.git('add', '.')
        fixture.git('commit', '-qm', 'rename and add')
        snapshot = {**fixture.manifest['snapshot'], 'head': fixture.git('rev-parse', 'HEAD')}
        session = SimpleNamespace(git=GitContext({**fixture.manifest, 'snapshot': snapshot}))
        refs = dict(base_sha=session.git.base, start_sha=session.git.base, head_sha=session.git.head)
        renamed = position(session, dict(path='renamed.txt', line=2), refs)
        self.assertEqual(('source.txt', 'renamed.txt'), (renamed['old_path'], renamed['new_path']))
        added = position(session, dict(path='added.txt', start_line=1, line=2), refs)
        self.assertEqual(hashlib.sha1(b'added.txt').hexdigest() + '_0_1', added['line_range']['start']['line_code'])
        fixture.git('rm', 'renamed.txt'); fixture.git('commit', '-qm', 'delete')
        session.git = GitContext({**fixture.manifest, 'snapshot': dict(head=fixture.git('rev-parse', 'HEAD'), merge_base=snapshot['head'])})
        refs.update(base_sha=session.git.base, start_sha=session.git.base, head_sha=session.git.head)
        deleted = position(session, dict(path='renamed.txt', side='LEFT', start_line=1, line=2), refs)
        self.assertIn('old_line', deleted)
        self.assertNotIn('new_line', deleted)

    def test_markdown_and_literal_code_survive_while_slash_directives_are_inert(self):
        body = '**Heading** @user #1 !1\r\n/close\n```sh\n/close\n```\n`\n/merge\n`\n'
        rendered = render_body(body)
        self.assertIn('/\u200bclose', rendered)
        self.assertIn('```sh\n/close\n```', rendered)
        self.assertIn('`\n/merge\n`', rendered)
        self.assertIn('**Heading** @user #1 !1', rendered)
        for body in ('`unmatched\n/close', '`unmatched\n\n/close\n`', '# Heading `\n/close\n`'):
            self.assertIn('/\u200bclose', render_body(body))

    def test_markup_inside_literal_blocks_cannot_exempt_later_commands(self):
        for prefix in ('```html\n<pre>\n```\n', '<pre>\n```\n</pre>\n',
                       '<!--\n```\n-->\n', '<textarea>\n```\n</textarea>\n',
                       '<div>\n```\n</div>\n\n', '    <!--\n\n', '    <?\n\n'):
            with self.subTest(prefix=prefix):
                self.assertTrue(render_body(prefix + '/close').endswith('/\u200bclose'))
        literal = '<pre>\n/close\n</pre>\n\n```sh\n/close\n```\n'
        self.assertEqual(literal + '/\u200bmerge', render_body(literal + '/merge'))
