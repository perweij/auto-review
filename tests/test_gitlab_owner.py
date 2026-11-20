"""GitLab Owner through the public API, real Git, and native-shaped transports."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_owner
from pr_review.application import dispatch
from pr_review.common import ApiError, read_json
from pr_review.forge import adapter
from pr_review.owner_state import OwnerSession


class GitLabOwnerTest(unittest.TestCase):
    def setUp(self):
        self.fx = test_owner.OwnerTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.forge = adapter('gitlab')
        self.identity = 'gitlab.com/group/sub/repo'
        self.url = 'https://' + self.identity + '/-/merge_requests/7'
        self.fx.git('remote', 'set-url', 'origin', 'https://' + self.identity + '.git')
        self.fx.git('config', 'url.' + str(self.fx.repo) + '.insteadOf', 'https://' + self.identity + '.git')
        self.fx.git('update-ref', 'refs/merge-requests/7/head', self.fx.head)
        self.raw = dict(iid=7, id=70, target_project_id=1, source_project_id=1, web_url=self.url,
                        title='Feature', description='', author={'username': 'author'}, state='opened', draft=False,
                        source_branch='feature', target_branch='main', sha=self.fx.head,
                        diff_refs=dict(head_sha=self.fx.head, start_sha=self.fx.base, base_sha=self.fx.base))
        self.threads = [dict(id='standalone', individual_note=True, notes=[self.note(101)]),
                        dict(id='diff', individual_note=False, notes=[self.note(102, resolvable=True,
                             position=dict(new_path='source.txt', new_line=1, base_sha=self.fx.base,
                                           start_sha=self.fx.base, head_sha=self.fx.head))])]
        self.writes, self.loss = [], None
        for target, side_effect in [('pr_review.forge_gitlab_owner.graphql', self.graphql),
                                    ('pr_review.forge_gitlab_publication.command', self.transport),
                                    ('pr_review.forge_gitlab_owner.command', self.transport)]:
            p = patch(target, side_effect=side_effect)
            p.start()
            self.addCleanup(p.stop)
        for name, side_effect in [('get', self.get), ('pages', lambda repo, endpoint: copy.deepcopy(self.threads)),
                                  ('account', lambda *args: dict(forge='gitlab', host='gitlab.com', username='author', id='9'))]:
            p = patch.object(self.forge, name, side_effect=side_effect)
            p.start()
            self.addCleanup(p.stop)
        self.view = dispatch(dict(command='owner-open', pr=self.url + '/diffs?view=parallel#note_1', cwd=str(self.fx.repo)))
        self.path = self.view['session']
        self.request('owner-refresh')

    def note(self, identity, **extra):
        return dict(id=identity, author=dict(id=9, username='author'), body='Please correct multiplication',
                    created_at='2026-11-18T00:00:00Z', updated_at='2026-11-18T00:00:00Z',
                    system=False, resolvable=False, resolved=False, project_id=1,
                    noteable_id=70, noteable_type='MergeRequest') | extra

    def get(self, repo, endpoint):
        if endpoint == self.forge.endpoint(repo):
            return copy.deepcopy(self.raw)
        if endpoint.startswith('projects/'):
            return {'path_with_namespace': 'group/sub/repo' if self.raw['source_project_id'] == 1 else 'fork/repo'}
        raise AssertionError(endpoint)

    def graphql(self, repo, query, **variables):
        nodes = []
        for thread in self.threads:
            root = thread['notes'][0]
            nodes.append(dict(id='gid://gitlab/Discussion/' + thread['id'],
                              replyId='gid://gitlab/' + ('IndividualNoteDiscussion/' if thread['individual_note'] else 'DiffDiscussion/') + thread['id'],
                              resolvable=root['resolvable'], resolved=root['resolved'],
                              userPermissions={'resolveNote': root['resolvable']}, notes=dict(
                              nodes=[dict(id='gid://gitlab/' + ('DiffNote/' if n.get('position') else 'DiscussionNote/' if not thread['individual_note'] else 'Note/') + str(n['id']),
                                          system=n['system'], resolvable=n['resolvable'], resolved=n['resolved'],
                                          userPermissions={'createNote': True}) for n in thread['notes']],
                              pageInfo=dict(hasNextPage=False, endCursor=None))))
        return dict(currentUser=dict(id='gid://gitlab/User/9', username='author'), project=dict(mergeRequest=dict(
                    iid='7', webUrl=self.url, userPermissions={'createNote': True},
                    discussions=dict(nodes=nodes, pageInfo=dict(hasNextPage=False, endCursor=None)))))

    def transport(self, args, **kwargs):
        if '--help' in args:
            return SimpleNamespace(stdout='--hostname --method --input')
        method = args[args.index('--method') + 1]
        endpoint = args[args.index('--method') + 2]
        payload = json.loads(kwargs['input_text'])
        self.writes.append((method, endpoint, payload))
        if self.loss == 'before':
            raise ApiError('command_failed', 'connection lost before response')
        if self.loss == 'rejected':
            raise ApiError('command_failed', 'GitLab: forbidden (HTTP 403)')
        thread = next(t for t in self.threads if '/discussions/' + t['id'] in endpoint)
        if method == 'PUT':
            for n in thread['notes']:
                if n['resolvable']:
                    n['resolved'] = payload['resolved']
                n['updated_at'] = 'changed-by-resolution'
            return SimpleNamespace(stdout=json.dumps(thread))
        self.assertEqual('POST', method)
        thread['individual_note'] = False
        thread['notes'][0]['updated_at'] = 'changed-by-conversion'
        note = self.note(1000 + len(self.writes), body=payload['body'])
        thread['notes'].append(note)
        if self.loss == 'after':
            raise ApiError('command_failed', 'response lost after accepted write')
        return SimpleNamespace(stdout=json.dumps(note))

    def request(self, command, **fields):
        self.view = dispatch(dict(command='owner-read', session=self.path))
        result = dispatch(dict(command=command, session=self.path, revision=self.view['revision'], **fields))
        self.view = result.get('owner', result)
        return result

    def reply(self, key='gitlab-note:101', body='Acknowledged\n/close'):
        self.request('owner-mutate', operation='reply', item=key, body=body)
        identity = self.view['replies'][-1]['id']
        self.request('owner-mutate', operation='stage', reply=identity)
        return identity

    def test_native_conversion_preview_and_fingerprint(self):
        original = self.view['feedback'][0]['fingerprint']
        identity = self.reply()
        prepared = self.request('owner-prepare')
        row = prepared['preview']['operations'][0]
        self.assertEqual('Acknowledged\n/\u200bclose', row['body'])
        self.assertTrue(row['creates_thread'])
        self.assertEqual('standalone', row['destination']['discussion_id'])
        target = copy.deepcopy(prepared['replies'][0]['target'])
        self.request('owner-publish', token=prepared['token'])
        self.assertEqual('published', self.view['replies'][-1]['status'])
        self.request('owner-refresh')
        item = next(i for i in self.view['feedback'] if i['key'] == 'gitlab-note:101')
        self.assertEqual(original, item['fingerprint'])
        self.assertEqual(target, item['reply_target'])
        self.assertEqual('thread', item['kind'])
        self.assertEqual(identity, self.view['replies'][-1]['id'])
        self.assertEqual('confirmed', self.view['actions'][0]['delivery'])
        self.assertIn('/discussions/standalone/notes', self.writes[0][1])

    def test_lost_response_recovers_without_duplicate(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.loss = 'after'
        with self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        self.request('owner-recover')
        self.assertEqual('published', self.view['replies'][-1]['status'])
        self.assertEqual(1, len(self.writes))

    def test_unknown_absence_blocks_every_new_publication(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.loss = 'before'
        with self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        self.request('owner-recover')
        self.assertEqual('ambiguous', self.view['actions'][0]['status'])
        self.assertEqual('unknown', self.view['actions'][0]['delivery'])
        with self.assertRaises(ApiError) as error:
            self.request('owner-prepare')
        self.assertEqual('uncertain_publication', error.exception.code)
        self.assertEqual(1, len(self.writes))

    def test_definitive_rejection_reuses_frozen_operation(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.loss = 'rejected'
        with self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        self.request('owner-recover')
        self.assertEqual('not_sent', self.view['actions'][0]['delivery'])
        self.loss = None
        self.request('owner-mutate', operation='stage', reply=self.view['replies'][-1]['id'])
        retry = self.request('owner-prepare')
        self.assertEqual(prepared['preview'], retry['preview'])
        self.request('owner-publish', token=retry['token'])
        self.assertEqual('published', self.view['replies'][-1]['status'])

    def test_edited_uncertain_receipt_blocks_resend(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.loss = 'after'
        with self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        self.threads[0]['notes'][-1]['body'] += ' edited remotely'
        with self.assertRaises(ApiError) as error:
            self.request('owner-recover')
        self.assertEqual('ambiguous_receipt', error.exception.code)
        self.assertEqual(1, len(self.writes))

    def test_thread_state_native_identity_and_semantic_fingerprint(self):
        item = next(i for i in self.view['feedback'] if i['root_id'] == 102)
        for resolved in (True, False):
            self.request('owner-thread-state', item=item['key'], resolved=resolved)
            current = next(i for i in self.view['feedback'] if i['key'] == item['key'])
            self.assertEqual(item['fingerprint'], current['fingerprint'])
            self.assertEqual(resolved, current['is_resolved'])
        self.assertTrue(all(e.endswith('/discussions/diff') for _, e, _ in self.writes))

    def test_fork_conversations_available_but_all_fix_entries_stop_early(self):
        self.raw['source_project_id'] = 2
        self.request('owner-refresh')
        self.assertFalse(self.view['fixing']['available'])
        with patch('pr_review.owner_git.prepare', side_effect=AssertionError('worktree prepared')), patch('pr_review.owner_providers.invoke', side_effect=AssertionError('model ran')):
            for command in ('owner-fix', 'owner-prepare-commit', 'owner-associate'):
                with self.subTest(command=command), self.assertRaises(ApiError) as error:
                    self.request(command, items=['gitlab-note:101'])
                self.assertEqual('fixing_unavailable', error.exception.code)
        self.reply()
        prepared = self.request('owner-prepare')
        self.request('owner-publish', token=prepared['token'])
        self.assertEqual('published', self.view['replies'][-1]['status'])

    def test_system_notes_permissions_and_nonresolvable_attention(self):
        self.threads.append(dict(id='system', individual_note=True, notes=[self.note(103, system=True)]))
        self.request('owner-refresh')
        item = next(i for i in self.view['feedback'] if i['root_id'] == 103)
        self.assertFalse(item['actionable'])
        self.assertFalse(item['can_reply'])
        self.reply()
        prepared = self.request('owner-prepare')
        self.request('owner-publish', token=prepared['token'])
        self.request('owner-refresh')
        self.request('owner-mutate', operation='restore', items=['gitlab-note:101'])
        self.request('owner-mutate', operation='acknowledge', items=['gitlab-note:101'])
        # Explicit queueing still requests attention; without queue/unread, a
        # non-resolvable discussion must not create attention by itself.
        session = OwnerSession(dict(session=self.path))
        state = session.load()
        entry = next(i for i in state['feedback'] if i['root_id'] == 101)
        entry.update(queued=False, unread=False)
        session.save(state)
        self.request('owner-refresh')
        self.assertFalse(next(i for i in self.view['feedback'] if i['root_id'] == 101)['attention'])

    def test_same_project_fixing_uses_shared_verification_and_commit_links(self):
        self.request('owner-mutate', operation='restore', items=['gitlab-note:102'])
        self.request('owner-fix', items=['gitlab-note:102'])
        self.assertEqual('ready', self.view['batches'][-1]['status'])
        worktree = self.view['worktree']
        self.fx.git('add', '.', cwd=worktree)
        self.fx.git('commit', '-qm', 'Address GitLab feedback', cwd=worktree)
        sha = self.fx.git('rev-parse', 'HEAD', cwd=worktree)
        self.fx.git('update-ref', 'refs/merge-requests/7/head', sha)
        self.raw['sha'] = self.raw['diff_refs']['head_sha'] = sha
        self.request('owner-associate')
        self.assertIn('/-/commit/' + sha, self.view['replies'][-1]['body'])
        self.assertNotIn('In response to', self.view['replies'][-1]['body'])
        prepared = self.request('owner-prepare')
        self.request('owner-publish', token=prepared['token'])
        self.request('owner-thread-state', item='gitlab-note:102', resolved=True)
        item = next(i for i in self.view['feedback'] if i['root_id'] == 102)
        self.assertIn('remote_completion', item)
        self.assertNotIn('github_completion', item)

    def test_local_identity_discovery_and_toolkit_do_not_read_forge(self):
        with patch.object(self.forge, 'get', side_effect=AssertionError('network')):
            found = dispatch(dict(command='owner-discover', cwd=str(self.fx.repo), pr=self.url + '/diffs', forge='gitlab'))
            self.assertEqual([self.path], [s['session'] for s in found['sessions']])
            dispatch(dict(command='toolkit', cwd=str(self.fx.repo), repository=self.identity, toolkit='owner'))
            self.assertEqual('owner', dispatch(dict(command='toolkit', cwd=str(self.fx.repo), repository=self.identity))['toolkit'])
            opened = dispatch(dict(command='owner-open', cwd=str(self.fx.repo), pr=7))
            self.assertEqual(self.path, opened['session'])

    def test_permission_failure_retains_cached_context(self):
        before = read_json(Path(self.path) / 'session.json')
        with patch('pr_review.forge_gitlab_owner.graphql', return_value={'project': None}), self.assertRaises(ApiError):
            self.request('owner-refresh')
        self.assertEqual(before, read_json(Path(self.path) / 'session.json'))

    def test_permission_join_retries_once_then_fails_closed(self):
        from pr_review.forge_gitlab_owner import discussion
        repo = OwnerSession(dict(session=self.path)).repo
        account = self.forge.account(repo.project, repo.cwd)
        graph = self.graphql(repo, '')
        missing = copy.deepcopy(graph)
        missing['project']['mergeRequest']['discussions']['nodes'].pop()
        with patch('pr_review.forge_gitlab_owner.graphql', side_effect=[missing, graph]) as query:
            self.assertEqual(2, len(discussion(self.forge, repo, account)[0]['items']))
            self.assertEqual(2, query.call_count)
        with patch('pr_review.forge_gitlab_owner.graphql', return_value=missing) as query, self.assertRaises(ApiError):
            discussion(self.forge, repo, account)
        self.assertEqual(2, query.call_count)

    def test_both_graphql_connection_levels_are_paginated(self):
        from pr_review.forge_gitlab_owner import permissions
        repo = OwnerSession(dict(session=self.path)).repo
        graph = self.graphql(repo, '')
        first, second = copy.deepcopy(graph), copy.deepcopy(graph)
        rows = graph['project']['mergeRequest']['discussions']['nodes']
        first['project']['mergeRequest']['discussions'] = dict(nodes=[copy.deepcopy(rows[0])], pageInfo=dict(hasNextPage=True, endCursor='discussion-next'))
        first['project']['mergeRequest']['discussions']['nodes'][0]['notes']['pageInfo'] = dict(hasNextPage=True, endCursor='notes-next')
        extra = copy.deepcopy(rows[0]['notes']['nodes'][0])
        extra['id'] = 'gid://gitlab/Note/500'
        notes_page = dict(node=dict(notes=dict(nodes=[extra], pageInfo=dict(hasNextPage=False, endCursor=None))))
        second['project']['mergeRequest']['discussions']['nodes'] = [rows[1]]
        with patch('pr_review.forge_gitlab_owner.graphql', side_effect=[first, notes_page, second]) as query:
            allowed, result = permissions(repo, self.forge.account(repo.project, repo.cwd))
        self.assertTrue(allowed)
        self.assertEqual({101, 500}, set(result['standalone']['native_notes']))
        self.assertEqual('notes-next', query.call_args_list[1].kwargs['after'])
        self.assertEqual('discussion-next', query.call_args_list[2].kwargs['after'])

    def test_comment_permissions_and_discussion_resolvability_are_authoritative(self):
        graph = self.graphql(None, '')
        graph['project']['mergeRequest']['userPermissions']['createNote'] = False
        with patch('pr_review.forge_gitlab_owner.graphql', return_value=graph):
            self.request('owner-refresh')
        item = next(i for i in self.view['feedback'] if i['root_id'] == 101)
        self.assertFalse(item['can_reply'])
        self.request('owner-mutate', operation='reply', item=item['key'], body='Draft retained locally')
        with self.assertRaises(ApiError) as error:
            self.request('owner-mutate', operation='stage', reply=self.view['replies'][-1]['id'])
        self.assertEqual('permission_denied', error.exception.code)
        self.assertEqual([], self.writes)

    def test_reviewer_discussion_bytes_and_graphql_independence(self):
        repo = OwnerSession(dict(session=self.path)).repo
        from pr_review.common import digest
        with patch('pr_review.forge_gitlab_owner.graphql', side_effect=AssertionError('Reviewer queried Owner permissions')):
            before, raw = self.forge.discussion(repo)
        self.request('owner-refresh')
        after, raw_after = self.forge.discussion(repo)
        self.assertEqual(digest(before), digest(after))
        self.assertEqual(raw, raw_after)
        self.assertEqual('comment:101', before['items'][0]['key'])
        self.assertNotIn('owner_semantic', before['items'][0])

    def test_duplicate_and_wrong_parent_receipts_block_recovery(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.loss = 'after'
        with self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        clone = copy.deepcopy(self.threads[0]['notes'][-1])
        clone['id'] += 1
        self.threads[1]['notes'].append(clone)
        with self.assertRaises(ApiError) as error:
            self.request('owner-recover')
        self.assertEqual('ambiguous_receipt', error.exception.code)
        self.threads[0]['notes'].pop()
        with self.assertRaises(ApiError) as error:
            self.request('owner-recover')
        self.assertEqual('ambiguous_receipt', error.exception.code)
        self.assertEqual(1, len(self.writes))

    def test_cli_initial_open_resume_and_url_validation_keep_forge(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from pr_review.owner_cli import main
        import os
        before = os.getcwd()
        os.chdir(self.fx.repo)
        self.addCleanup(os.chdir, before)
        for args in ([self.url + '/diffs?x=1#note_101', 'open', '--forge', 'gitlab'],
                     ['read', '--session', self.path, '--forge', 'gitlab'],
                     ['read', '--session', self.path, '--repo', 'group/sub/repo']):
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                self.assertEqual(0, main(args))
            self.assertEqual(self.path, json.loads(output.getvalue())['session'])

    def test_reviewer_handoff_preserves_identity_and_rejects_forks_before_open(self):
        from pr_review.storage import write_json
        from pr_review.state import Session
        review_path = self.fx.saved_review().root
        manifest = read_json(review_path / 'manifest.json')
        manifest.update(repository=self.identity, forge_project=self.forge.project(self.identity).saved(), url=self.url,
                        change=self.forge.change(OwnerSession(dict(session=self.path)).repo))
        write_json(review_path / 'manifest.json', manifest)
        review = Session(dict(session=str(review_path)))
        view = review.view(review.load())
        comment = view['comments'][0]['id']
        owner = OwnerSession(dict(session=self.path))
        cached = owner.load()
        cached.pop('change', None)
        owner.save(cached)
        result = dispatch(dict(command='owner-import', session=str(review_path), generation=view['generation'], revision=view['revision'], comments=[comment]))
        self.assertEqual(self.path, result['owner']['session'])
        self.assertEqual('gitlab', result['owner']['forge_project']['forge'])
        self.assertTrue(result['owner']['fixing']['available'])
        self.raw['source_project_id'] = 2
        with patch('pr_review.owner_import.open_session', side_effect=AssertionError('Owner opened')), self.assertRaises(ApiError) as error:
            dispatch(dict(command='owner-import', session=str(review_path), generation=view['generation'], revision=view['revision'], comments=[comment]))
        self.assertEqual('fixing_unavailable', error.exception.code)

    def test_partial_batch_retains_confirmed_reply_and_previews_only_remainder(self):
        self.reply(body='First reply')
        second = self.reply('gitlab-note:102', body='Second reply')
        prepared = self.request('owner-prepare')
        transport = self.transport
        def fail_second(args, **kwargs):
            if '--help' not in args and args[args.index('--method') + 1] == 'POST':
                self.loss = 'rejected' if '/discussions/diff/notes' in args[args.index('--method') + 2] else None
            return transport(args, **kwargs)
        with patch('pr_review.forge_gitlab_publication.command', side_effect=fail_second), self.assertRaises(ApiError):
            self.request('owner-publish', token=prepared['token'])
        self.request('owner-recover')
        self.assertEqual('published', self.view['replies'][0]['status'])
        self.request('owner-mutate', operation='stage', reply=second)
        remaining = self.request('owner-prepare')
        self.assertEqual(['Second reply'], [r['body'] for r in remaining['preview']['operations']])
        self.loss = None
        self.request('owner-publish', token=remaining['token'])
        self.assertTrue(all(r['status'] == 'published' for r in self.view['replies']))
        self.assertEqual(2, len(self.threads[0]['notes']))

    def test_changed_actor_and_feedback_fail_before_writes(self):
        self.reply()
        prepared = self.request('owner-prepare')
        self.threads[0]['notes'][0]['body'] += ' changed by author'
        with self.assertRaises(ApiError) as error:
            self.request('owner-publish', token=prepared['token'])
        self.assertEqual('stale_discussion', error.exception.code)
        self.assertEqual([], self.writes)
        with patch.object(self.forge, 'account', return_value=dict(forge='gitlab', host='gitlab.com', username='author', id='10')), self.assertRaises(ApiError):
            self.request('owner-refresh')
        self.assertEqual([], self.writes)

    def test_saved_owner_rejects_repository_and_forge_conflicts(self):
        for fields in (dict(repository='gitlab.com/other/project'), dict(forge='github')):
            with self.subTest(fields=fields), self.assertRaises(ApiError):
                dispatch(dict(command='owner-read', session=self.path, **fields))
