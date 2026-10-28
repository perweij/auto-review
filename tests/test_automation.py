"""Automatic review contracts with real saved Git snapshots and mocked GitHub."""
import copy
from pathlib import Path
import shutil
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, call, patch

import test_api
from pr_review import automation, processes, publication
from pr_review.application import dispatch
from pr_review.commands import mutate
from pr_review.common import ApiError, atomic_json, digest, read_json, state_root
from pr_review.state import Session


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.f = test_api.ApiTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.f.manifest['draft_mutations_tracked'] = True
        atomic_json(self.f.path / 'manifest.json', self.f.manifest)
        self.session = Session({'session': str(self.f.path)})
        self.op = {'operation_id': 'a' * 32, 'log_path': str(state_root() / 'operations' / ('a' * 32 + '.log'))}
        self.run = automation.AutomaticRun(self.op)

    def saved(self, change=None):
        state = self.session.load()
        if change:
            change(state)
        self.session.save(state)
        return state

    def publish(self, **options):
        github, posts = self.f.fake_github(**options)
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            result = self.run.publish(self.session)
        return result, posts

    def automatic(self, execute_error=None):
        from pr_review import models, reviews, settings
        from pr_review.repository import Repository
        repo = SimpleNamespace(common=self.f.repo / '.git', pr=7, lock=self.session.pr_lock)
        with patch.object(Repository, 'resolve', return_value=repo), \
                patch('pr_review.sessions.locate', return_value=(self.session.root, self.f.repo)), \
                patch.object(settings, 'resolve', return_value={'settings': settings.DEFAULTS}), \
                patch.object(models, 'discover'), patch.object(models, 'validate_selection'), \
                patch.object(reviews, 'execute', return_value=self.session, side_effect=execute_error):
            return automation.run({'pr': 7})

    def github_with_urls(self):
        github, posts = self.f.fake_github()
        def call(*args, **kwargs):
            result = github(*args, **kwargs)
            for receipt in result if isinstance(result, list) else [result]:
                receipt['html_url'] = f"https://test/receipt/{receipt['id']}"
            return result
        return call, posts

    def reply_then_review(self):
        state = self.session.load()
        state['discussion']['items'] = [dict(key='thread:test', kind='thread', root_id=88, can_reply=True)]
        atomic_json(self.f.path / 'discussion/snapshot-a/discussion.json', state['discussion'])
        state['comments'][0]['placement'] = dict(kind='reply', key='thread:test', root_id=88)
        self.session.save(state)

    def test_invalid_suggestion_blocks_the_whole_batch_then_verified_payload_recovers(self):
        from pr_review import suggestions
        state = self.session.load()
        comment = state['comments'][0]
        target = self.session.git.anchor(comment['placement'])
        comment['placement'] = target
        comment['suggestion'] = dict(head=self.session.git.head, target=target,
                                     original='first\n', replacement='fixed\n')
        comment['suggestion_generation'] = {'status': 'generated'}
        self.session.save(state)
        github, posts = self.github_with_urls()
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            result = self.automatic()
        self.assertEqual(result['outcome'], 'needs_attention')
        self.assertEqual(posts, [])
        self.assertTrue(all(c['status'] == 'unstaged' for c in self.session.load()['comments']))
        state = self.session.load()
        comment = state['comments'][0]
        comment['suggestion']['verification'] = dict(status='accepted', fingerprint=suggestions.identity(self.session, comment))
        self.session.save(state)
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            result = self.automatic()
            self.assertEqual(result['outcome'], 'published')
            self.assertEqual(self.automatic()['outcome'], 'noop')
        self.assertEqual(len(posts), 1)
        self.assertIn('```suggestion\nfixed\n```', posts[0][1]['comments'][0]['body'])

    def assert_saved_outcome(self, result):
        self.assertEqual(read_json(self.session.root / 'automatic-runs' / (result['operation_id'] + '.json')), result)
        operation = read_json(state_root() / 'operations' / (result['operation_id'] + '.json'))
        self.assertEqual(operation['exit_code'], result['exit_code'])
        self.assertEqual(operation['status'], 'cancelled' if result['outcome'] == 'interrupted' else 'stopped')

    def test_late_cancellation_preserves_published_and_noop_records(self):
        github, posts = self.github_with_urls()
        finish = automation.AutomaticRun.finish
        completed = []
        def late_signal(controller, *args, **kwargs):
            result = finish(controller, *args, **kwargs)
            completed.append(copy.deepcopy(result))
            processes._cancellation.get().request(signal.SIGTERM)
            return result
        for outcome in ('published', 'noop'):
            with self.subTest(outcome=outcome), \
                    patch.object(publication, 'repository_operation'), \
                    patch.object(publication, 'gh', side_effect=github), \
                    patch.object(automation.AutomaticRun, 'finish', autospec=True, side_effect=late_signal):
                result = self.automatic()
                self.assertEqual(result, completed[-1])
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['exit_code'], 0)
                self.assertEqual(result['receipts'], ['https://test/receipt/200', 'https://test/receipt/300',
                                                      'https://test/receipt/301'])
                self.assert_saved_outcome(result)
        self.assertEqual(len(completed), 2)
        self.assertEqual(len(posts), 1)

    def test_cancellation_during_submission_retains_confirmed_receipts(self):
        self.reply_then_review()
        github, posts = self.github_with_urls()
        def cancel_review(session, endpoint, payload=None, **kwargs):
            if endpoint == 'reviews' and payload is not None:
                processes._cancellation.get().request(signal.SIGTERM)
                processes._cancellation.get().check()
            return github(session, endpoint, payload, **kwargs)
        with patch.object(publication, 'repository_operation'), \
                patch.object(publication, 'gh', side_effect=cancel_review):
            result = self.automatic()
        self.assertEqual(result['outcome'], 'uncertain')
        self.assertEqual(result['exit_code'], 5)
        self.assertEqual(result['receipts'], ['https://test/receipt/101'])
        pending = automation.actions(self.session.load())[1]
        self.assertEqual(result['affected'], [dict(id=pending['id'], status='ambiguous', comment_ids=['p2'])])
        self.assertEqual(len(posts), 1)
        self.assert_saved_outcome(result)

    def test_cancellation_between_posts_reports_partial_with_unsent_remainder(self):
        self.reply_then_review()
        github, posts = self.github_with_urls()
        def cancel_after_reply(session, operation, verdict='COMMENT'):
            if operation == 'validate' and any(a['status'] == 'published' for a in automation.actions(session.load())):
                processes._cancellation.get().request(signal.SIGINT)
                processes._cancellation.get().check()
        with patch.object(publication, 'repository_operation', side_effect=cancel_after_reply), \
                patch.object(publication, 'gh', side_effect=github):
            result = self.automatic()
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['exit_code'], 4)
        self.assertEqual(result['receipts'], ['https://test/receipt/101'])
        pending = automation.actions(self.session.load())[1]
        self.assertEqual(result['affected'], [dict(id=pending['id'], status='draft', comment_ids=['p2'])])
        self.assertEqual(len(posts), 1)
        self.assert_saved_outcome(result)

    def test_cancellation_during_inline_receipt_lookup_keeps_parent_review_url(self):
        github, posts = self.github_with_urls()
        def cancel_lookup(session, endpoint, *args, **kwargs):
            if endpoint == 'reviews/200/comments':
                raise processes.Cancelled(signal.SIGTERM)
            return github(session, endpoint, *args, **kwargs)
        with patch.object(publication, 'repository_operation'), \
                patch.object(publication, 'gh', side_effect=cancel_lookup):
            result = self.automatic()
        self.assertEqual(result['outcome'], 'uncertain')
        self.assertEqual(result['receipts'], ['https://test/receipt/200'])
        self.assertEqual(result['affected'][0]['status'], 'ambiguous')
        self.assertEqual(len(posts), 1)
        self.assert_saved_outcome(result)

    def test_cancellation_during_recovery_preserves_state_and_does_not_resend(self):
        initial = self.session.load()
        for status, outcome, code in (('submitting', 'uncertain', 5), ('ambiguous', 'uncertain', 5),
                                      ('failed', 'partial', 4)):
            with self.subTest(status=status):
                state = copy.deepcopy(initial)
                state['batches'] = [{'actions': [dict(id='sent', kind='reply', status='ambiguous', comment_ids=['p1'],
                                                     target={'root_id': 88}, marker='saved-marker', viewer='reviewer'),
                                                dict(id='pending', kind='review', status=status, comment_ids=['p2'])]}]
                self.session.save(state)
                def recover_then_cancel(session, endpoint, *, pages):
                    if endpoint == 'comments':
                        return [dict(id=101, user={'login': 'reviewer'}, body='saved-marker', in_reply_to_id=88,
                                     html_url='https://test/recovered')]
                    raise processes.Cancelled(signal.SIGTERM)
                with patch.object(publication, 'gh', side_effect=recover_then_cancel) as github:
                    result = self.automatic()
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['exit_code'], code)
                self.assertEqual(result['receipts'], ['https://test/recovered'])
                self.assertEqual(result['affected'], [dict(id='pending', status=status, comment_ids=['p2'])])
                self.assertEqual(github.call_args_list, [call(ANY, 'comments', pages=True), call(ANY, 'reviews', pages=True)])
                self.assertEqual(self.session.load()['comments'][0]['receipt']['html_url'], 'https://test/recovered')
                self.assert_saved_outcome(result)

    def test_analysis_cancellation_retains_signal_exit_code(self):
        result = self.automatic(execute_error=processes.Cancelled(signal.SIGHUP))
        self.assertEqual(result['outcome'], 'interrupted')
        self.assertEqual(result['exit_code'], 129)
        self.assertEqual(result['receipts'], [])
        self.assertEqual(result['affected'], [])
        self.assert_saved_outcome(result)

    def test_rebinding_generation_clears_old_publication_state(self):
        self.run.bind(self.session)
        state = self.session.load()
        state['batches'] = [{'actions': [dict(id='old', status='ambiguous')]}]
        self.run.state = state
        self.run.bind(Session({'session': str(self.session.root)}))
        self.assertIs(self.run.state, state)
        # Analysis mutates its Store in place before observe() rebinds it.
        store = SimpleNamespace(root=self.session.root, path=self.session.path, manifest=self.session.manifest)
        self.run.bind(store)
        store.path = self.session.path / 'new-generation'
        self.run.bind(store)
        result = self.run.interrupt(130)
        self.assertIsNone(self.run.state)
        self.assertEqual(result['outcome'], 'interrupted')
        self.assertEqual(result['affected'], [])

    def test_unchanged_observations_skip_writes_and_refresh_store(self):
        from pr_review.sessions import Store
        store = Store.__new__(Store)
        store.root, store.path = self.session.root, self.session.path
        store.manifest = copy.deepcopy(self.session.manifest)
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        with patch.object(automation, 'atomic_json', wraps=atomic_json) as writes:
            for _ in range(3):
                store.save()
            state = self.run.state = self.session.load()
            equivalent = Session({'session': str(self.session.root)})
            automation.observe(equivalent)
            self.assertIs(self.run.store, equivalent)
            self.assertIs(self.run.state, state)
            self.assertEqual([c.args[0] for c in writes.call_args_list], [
                state_root() / 'operations' / (self.op['operation_id'] + '.json'),
                self.session.root / 'automatic-runs' / (self.op['operation_id'] + '.json')])
            # Explicit persistence is independent of association changes.
            self.run.record['reason'] = 'Progress saved explicitly'
            self.run.save()
            self.assertEqual(writes.call_count, 3)
            result = self.run.finish('noop', 'Finished without rebinding')
            self.assertEqual(writes.call_count, 4)
            self.assertEqual(read_json(writes.call_args.args[0]), result)

    def test_binding_transitions_persist_root_path_and_generation(self):
        store = SimpleNamespace(root=self.session.root, path=self.session.path,
                                manifest=copy.deepcopy(self.session.manifest))
        with patch.object(automation, 'atomic_json', wraps=atomic_json) as writes:
            self.run.bind(store)
            for field, value in (('root', self.f.root / 'other-session'),
                                 ('path', self.session.path / 'replacement'),
                                 ('run_id', 'new-run')):
                with self.subTest(field=field):
                    previous_writes = writes.call_count
                    self.run.state = self.session.load()
                    if field == 'run_id':
                        store.manifest[field] = value
                    else:
                        setattr(store, field, value)
                    self.run.bind(store)
                    self.assertIsNone(self.run.state)
                    self.assertEqual(writes.call_count, previous_writes + 2)
                    self.assertEqual(read_json(writes.call_args_list[-2].args[0])['session'], str(store.root))
                    record = read_json(store.root / 'automatic-runs' / (self.op['operation_id'] + '.json'))
                    self.assertEqual(record['session'], str(store.root))
                    self.assertEqual(record['generation'], automation.generation(store.path, store.manifest))
                    self.run.bind(store)
                    self.assertEqual(writes.call_count, previous_writes + 2)

    def test_preflight_duplicate_bindings_write_associations_once(self):
        self.saved()
        with patch.object(automation, 'atomic_json', wraps=atomic_json) as writes:
            self.assertIsNone(self.run.before_analysis(self.session.root))
            self.assertIsNone(self.run.before_analysis(self.session.root))
        self.assertEqual(writes.call_count, 2)
        self.assertEqual(self.run.store.path, self.session.path)
        self.assertIsNotNone(self.run.state)

    def generated_duplicates(self, *ids):
        file = self.f.path / 'publication/contributions.json'
        ledger = read_json(file)
        for proposal in ledger['proposals']:
            if proposal['id'] in ids:
                proposal.update(classification='DUPLICATE', body_md='')
                proposal['basis'].update(classification='DUPLICATE', body_md='')
        atomic_json(file, ledger)

    def test_comment_publication_and_repeat_noop(self):
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]['event'], 'COMMENT')
        self.assertTrue(all(c['receipt'] for c in self.session.load()['comments']))
        self.assertTrue(all(c['mutation_actor'] == 'automatic' for c in self.session.load()['comments']))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertFalse(posts)

    def test_each_blocker_stops_all_before_staging(self):
        changes = [dict(classification='UNCERTAIN'), dict(classification='REVISIT'),
                   dict(body=''), dict(placement={'kind': 'unplaced'}),
                   dict(placement={'kind': 'inline', 'path': 'missing', 'side': 'RIGHT', 'line': 2}),
                   dict(partial_coverage=True), dict(needs_attention='Discussion changed')]
        initial = self.session.load()
        for changeset in changes:
            with self.subTest(changes=changeset):
                state = copy.deepcopy(initial)
                state['comments'][1].update(changeset)
                self.session.save(state)
                before = self.session.file.read_bytes()
                result, posts = self.publish()
                self.assertEqual(result['exit_code'], 3)
                self.assertEqual([c['id'] for c in result['affected']], ['p2'])
                self.assertFalse(posts)
                self.assertEqual(before, self.session.file.read_bytes())

    def test_outdated_and_resolved_replies_need_judgment(self):
        for key in ('is_outdated', 'is_resolved'):
            state = self.session.load()
            state['discussion']['items'] = [dict(key='thread:T', kind='thread', root_id=9, can_reply=True,
                                                **{key: True})]
            state['comments'][0]['placement'] = dict(kind='reply', key='thread:T', root_id=9)
            self.session.save(state)
            result, posts = self.publish()
            self.assertEqual(result['exit_code'], 3)
            self.assertFalse(posts)

    def test_automatic_duplicates_and_empty_findings_are_noops(self):
        self.generated_duplicates('p1', 'p2')
        comments = self.session.load()['comments']
        self.assertEqual([c['body'] for c in comments], ['Comment 1', 'Comment 2'])
        self.assertTrue(all(c['status'] == 'suppressed' and not c['human_edited'] for c in comments))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(posts)
        self.saved(lambda s: s.update(comments=[]))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertFalse(posts)

    def test_generated_duplicate_does_not_block_mixed_publication(self):
        self.generated_duplicates('p1')
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0][1]['comments']), 1)
        self.assertEqual(posts[0][1]['comments'][0]['body'].split('\n\n<!--')[0], 'Comment 2')
        duplicate, published = self.session.load()['comments']
        self.assertEqual(duplicate['status'], 'suppressed')
        self.assertFalse(duplicate['human_edited'])
        self.assertEqual(published['status'], 'published')

    def test_generated_duplicate_does_not_block_replacement_before_publication(self):
        self.generated_duplicates('p1')
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        self.assertIsNone(self.run.before_analysis(self.session.root))
        self.assertIsNone(automation.guard_replacement(self.session))
        self.assertFalse(self.session.file.exists())

    def test_edited_duplicate_still_requires_manual_completion(self):
        self.generated_duplicates('p1')
        file = self.f.path / 'publication/contributions.json'
        ledger = read_json(file)
        ledger['proposals'][0]['body_md'] = 'Human duplicate wording'
        atomic_json(file, ledger)
        state = self.saved()
        self.assertTrue(state['comments'][0]['human_edited'])
        self.assertEqual(self.session.load()['comments'][0]['body'], 'Human duplicate wording')
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            automation.guard_replacement(self.session)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('manual decisions', result['reason'])
        self.assertFalse(posts)

    def test_manual_mutations_and_verdict_prevent_noop_or_publication(self):
        initial = self.session.load()
        for request in ({'operation': 'edit', 'comment': 'p1', 'body': 'Human wording'},
                        {'operation': 'stage', 'comment': 'p1'},
                        {'operation': 'suppress', 'comment': 'p1'},
                        {'operation': 'acknowledge', 'comment': 'p1'},
                        {'operation': 'placement', 'comment': 'p1', 'placement': {'kind': 'review'}},
                        {'operation': 'verdict', 'verdict': 'COMMENT'}):
            with self.subTest(request=request):
                state = copy.deepcopy(initial)
                mutate(self.session, state, {**request, 'actor': 'automatic'})
                result, posts = self.publish()
                self.assertEqual(result['exit_code'], 3)
                self.assertFalse(posts)
        state = copy.deepcopy(initial)
        state.update(comments=[], verdict_requested=True, verdict='APPROVE')
        self.session.save(state)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(posts)

    def test_legacy_saved_drafts_require_manual_completion(self):
        self.saved(lambda s: s.pop('mutation_tracking'))
        with self.assertRaisesRegex(ApiError, 'unknown'):
            self.run.before_analysis(self.session.root)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('unknown', result['reason'])
        self.assertFalse(posts)

    def test_completed_legacy_history_allows_automatic_analysis(self):
        initial = self.session.load()
        initial.pop('mutation_tracking')
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        for statuses in (('published', 'published'), ('suppressed', 'suppressed'),
                         ('published', 'suppressed'), ()):
            with self.subTest(statuses=statuses):
                state = copy.deepcopy(initial)
                state['comments'] = state['comments'][:len(statuses)]
                for comment, status in zip(state['comments'], statuses):
                    comment['status'] = status
                self.session.save(state)
                before = self.session.file.read_bytes()
                with patch.object(publication, 'gh', side_effect=AssertionError('No GitHub calls needed')):
                    self.assertIsNone(self.run.before_analysis(self.session.root))
                    automation.check_existing(self.session.root)
                    automation.guard_replacement(self.session)
                self.assertEqual(self.session.file.read_bytes(), before)

    def test_completed_legacy_history_still_blocks_direct_publication(self):
        state = self.session.load()
        state.pop('mutation_tracking')
        for comment in state['comments']:
            comment['status'] = 'published'
        self.session.save(state)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('unknown', result['reason'])
        self.assertFalse(posts)

    def test_completed_legacy_history_preserves_unfinished_batch_guard(self):
        state = self.session.load()
        state.pop('mutation_tracking')
        for comment in state['comments']:
            comment['status'] = 'suppressed'
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        for status in sorted(automation.OUTSTANDING):
            with self.subTest(status=status):
                state['batches'] = [{'actions': [dict(id='pending', status=status, comment_ids=['p1'])]}]
                self.session.save(state)
                with self.assertRaisesRegex(ApiError, 'unfinished batch'):
                    automation.check_existing(self.session.root)
                with self.assertRaisesRegex(ApiError, 'unfinished batch'):
                    automation.guard_replacement(self.session)
                with patch.object(publication, 'recover') as recover:
                    result = self.run.before_analysis(self.session.root)
                recover.assert_called_once()
                self.assertIn(result['outcome'], ('needs_attention', 'uncertain'))

    def test_completed_legacy_history_preserves_selected_verdict_guard(self):
        state = self.session.load()
        state.pop('mutation_tracking')
        state['verdict_requested'] = True
        for comment in state['comments']:
            comment['status'] = 'published'
        for verdict in ('COMMENT', 'APPROVE', 'REQUEST_CHANGES'):
            with self.subTest(verdict=verdict):
                state['verdict'] = verdict
                self.session.save(state)
                with self.assertRaisesRegex(ApiError, 'verdict was selected manually'):
                    self.run.before_analysis(self.session.root)

    def test_read_only_inspection_leaves_drafts_and_run_record_unchanged(self):
        self.saved(lambda s: s['comments'][0].update(classification='UNCERTAIN'))
        self.publish()
        files = [self.session.file, *self.session.root.joinpath('automatic-runs').glob('*.json')]
        before = {p: p.read_bytes() for p in files}
        first = dispatch({'command': 'read', 'session': str(self.session.root)})
        second = dispatch({'command': 'read', 'session': str(self.session.root)})
        self.assertEqual(first, second)
        self.assertEqual(first['analysis']['displayed']['status'], 'complete')
        self.assertEqual(first['automation']['outcome'], 'needs_attention')
        self.assertEqual(before, {p: p.read_bytes() for p in files})

    def test_lost_response_is_read_back_without_resending(self):
        result, posts = self.publish(ambiguous_review=True)
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(len(posts), 1)

    def test_unsent_remainder_survives_repeated_automatic_attempt(self):
        github, posts = self.f.fake_github()
        def unavailable(session, endpoint, *args, **kwargs):
            if args:
                raise ApiError('network', 'No response')
            return github(session, endpoint, *args, **kwargs)
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=unavailable):
            first = self.run.publish(self.session)
            second = self.run.before_analysis(self.session.root)
        self.assertEqual(first['exit_code'], 3)
        self.assertEqual(second['exit_code'], 3)
        self.assertFalse(posts)
        self.assertEqual(automation.actions(self.session.load())[0]['status'], 'failed')

    def test_uncertainty_takes_precedence_when_receipt_lookup_fails(self):
        def unavailable(*args, **kwargs):
            raise ApiError('network', 'Unavailable')
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=unavailable):
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 5)

    def test_mid_batch_draft_remainder_is_not_archived(self):
        state = self.saved()
        state['batches'] = [{'actions': [{'status': 'published', 'result': {'html_url': 'https://test/receipt'}},
                                       {'status': 'draft'}]}]
        self.session.save(state)
        with patch.object(publication, 'gh', side_effect=AssertionError('No lookup needed')):
            result = self.run.before_analysis(self.session.root)
        self.assertEqual(result['exit_code'], 4)
        self.assertEqual(automation.actions(self.session.load())[1]['status'], 'draft')

    def test_prepare_change_never_publishes_reduced_selection(self):
        def prepare(session, state):
            state['comments'][0]['status'] = 'unstaged'
            session.save(state)
            return {'ready': True, 'confirmation': 'token'}
        with patch.object(publication, 'prepare', side_effect=prepare), patch.object(publication, 'publish') as publish:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        publish.assert_not_called()

    def test_paused_record_does_not_create_empty_draft_ledger(self):
        (self.f.path / 'publication/contributions.json').unlink()
        self.f.manifest['status'] = 'paused'
        atomic_json(self.f.path / 'manifest.json', self.f.manifest)
        session = Session({'session': str(self.f.path)})
        result = self.run.publish(session)
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(session.file.exists())
        atomic_json(self.f.path / 'publication/contributions.json', {'proposals': [
            {'id': 'p1', 'basis': {'finding_ids': ['F1'], 'body_md': 'Retained finding'},
             'body_md': 'Retained finding', 'classification': 'NEW'}]})
        self.assertEqual(session.load()['comments'][0]['body'], 'Retained finding')

    def test_stale_generation_and_manual_replacement_guard(self):
        self.saved()
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        state = self.session.load()
        mutate(self.session, state, {'operation': 'edit', 'comment': 'p1', 'body': 'Do not replace'})
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            automation.guard_replacement(self.session)

    def test_generation_guards_check_current_and_pending_manual_drafts(self):
        self.saved()
        store = self.replacement()
        pending = Session({'session': str(store.root),
                           'generation': automation.generation(store.path, store.manifest)})
        state = pending.load()
        state['comments'][0]['human_edited'] = True
        pending.save(state)
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        with patch.object(publication, 'recover') as recover:
            for check in (lambda: self.run.before_analysis(self.session.root),
                          lambda: automation.check_existing(self.session.root)):
                with self.assertRaisesRegex(ApiError, 'manual decisions'):
                    check()
            # Replacement guards inspect the current generation, not the candidate.
            self.assertIsNone(automation.guard_replacement(self.session))
            self.saved(lambda s: s['comments'][0].update(human_edited=True))
            with self.assertRaisesRegex(ApiError, 'manual decisions'):
                automation.guard_replacement(self.session)
        recover.assert_not_called()

    def test_generation_guards_skip_copied_and_absent_draft_ledgers(self):
        clean = self.saved()
        self.saved(lambda s: s['comments'][0].update(human_edited=True))
        store = self.replacement()
        self.session.save(clean)
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        for ledger in ('copied', 'absent'):
            with self.subTest(ledger=ledger):
                if ledger == 'absent':
                    (store.path / 'publication/drafts.json').unlink()
                    (store.path / 'publication/contributions.json').unlink()
                before = {p: p.read_bytes() for p in (store.path / 'publication').iterdir()
                          if p.is_file() and p.name != '.drafts.lock'}
                with patch.object(publication, 'recover') as recover:
                    self.assertIsNone(self.run.before_analysis(self.session.root))
                    automation.check_existing(self.session.root)
                    automation.guard_replacement(self.session)
                recover.assert_not_called()
                self.assertEqual(self.run.store.path, self.session.path)
                self.assertEqual(before, {p: p.read_bytes() for p in (store.path / 'publication').iterdir()
                                          if p.is_file() and p.name != '.drafts.lock'})

    def test_generation_guard_recovers_and_checks_the_same_loaded_state(self):
        load = Session.load
        for manual_edit in (False, True):
            with self.subTest(manual_edit=manual_edit):
                state = self.saved()
                state['batches'] = [{'actions': [dict(id='pending', status='ambiguous')]}]
                self.session.save(state)
                def recover(session, recovered):
                    recovered['batches'][0]['actions'][0]['status'] = 'published'
                    recovered['comments'][0]['human_edited'] = manual_edit
                with patch.object(Session, 'load', autospec=True, side_effect=load) as loads, \
                        patch.object(publication, 'recover', side_effect=recover) as recovery:
                    if manual_edit:
                        with self.assertRaisesRegex(ApiError, 'manual decisions'):
                            self.run.before_analysis(self.session.root)
                    else:
                        self.assertIsNone(self.run.before_analysis(self.session.root))
                self.assertEqual(loads.call_count, 1)
                recovery.assert_called_once()
                self.assertIs(self.run.state, recovery.call_args.args[1])
                # Recovery in this test is deliberately in memory only.
                self.assertEqual(automation.actions(self.session.load())[0]['status'], 'ambiguous')

    def test_generation_guards_preserve_legacy_actions_without_contributions(self):
        (self.session.path / 'publication/contributions.json').unlink()
        atomic_json(self.session.path / 'publication/actions.json',
                    [dict(id='pending', kind='review', event='COMMENT', status='failed')])
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        with patch.object(publication, 'recover') as recover:
            for check in (lambda: automation.check_existing(self.session.root),
                          lambda: automation.guard_replacement(self.session)):
                with self.assertRaisesRegex(ApiError, 'unfinished batch'):
                    check()
            recover.assert_not_called()
            result = self.run.before_analysis(self.session.root)
        recover.assert_called_once()
        self.assertEqual(result['outcome'], 'needs_attention')
        self.assertEqual(automation.actions(recover.call_args.args[1])[0]['id'], 'pending')

    def test_generation_preflight_recovers_pending_but_rechecks_do_not(self):
        self.saved()
        store = self.replacement()
        pending = Session({'session': str(store.root),
                           'generation': automation.generation(store.path, store.manifest)})
        initial = pending.load()
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        cases = [('draft', False, None, 'needs_attention'),
                 ('failed', True, None, 'partial'),
                 ('submitting', False, None, 'uncertain'),
                 ('ambiguous', False, None, 'uncertain'),
                 ('failed', False, ApiError('network', 'Unavailable'), 'uncertain')]
        for status, published, error, outcome in cases:
            with self.subTest(status=status, published=published, error=error):
                state = copy.deepcopy(initial)
                batch = [dict(id='pending', status=status)]
                if published:
                    batch.insert(0, dict(id='posted', status='published'))
                state['batches'] = [{'actions': batch}]
                pending.save(state)
                with patch.object(publication, 'recover', side_effect=error) as recover:
                    with self.assertRaisesRegex(ApiError, 'unfinished batch'):
                        automation.check_existing(self.session.root)
                    recover.assert_not_called()
                    result = self.run.before_analysis(self.session.root)
                recover.assert_called_once()
                self.assertEqual(recover.call_args.args[0].path, pending.path)
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['generation'], pending.id)

    def test_saved_log_is_session_scoped(self):
        self.run.bind(self.session)
        path = state_root() / 'operations' / ('a' * 32 + '.log')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Saved model progress\n')
        result = dispatch({'command': 'review-log', 'session': str(self.session.root), 'operation_id': 'a' * 32})
        self.assertEqual(result['text'], path.read_text())
        for identity in ('../secret', 'b' * 32):
            with self.assertRaises(ApiError):
                dispatch({'command': 'review-log', 'session': str(self.session.root), 'operation_id': identity})

    def test_interrupted_record_is_reported_without_rewriting_it(self):
        self.run.bind(self.session)
        path = next((self.session.root / 'automatic-runs').glob('*.json'))
        before = path.read_bytes()
        self.assertEqual(automation.summary(self.session)['outcome'], 'interrupted')
        self.assertEqual(before, path.read_bytes())

    def trust_stop(self):
        self.run.bind(self.session)
        self.run.finish('needs_attention', 'Trust required', error_code='confirmation_required',
                        trusted_snapshot='head:base', agent_config_paths=['AGENTS.md'])
        path = self.session.root / 'automatic-runs' / (self.op['operation_id'] + '.json')
        return path, path.read_bytes()

    def replacement(self):
        from pr_review.sessions import Store
        from pr_review.storage import hash_file
        candidate = Path(str(self.session.root) + '-history') / 'attempt-manual'
        shutil.copytree(self.session.root, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest.update(session_directory=str(self.session.root), status='running',
                        attempt={'status': 'building', 'previous': str(self.session.path)},
                        phases={'synthesis': {'status': 'completed',
                                             'output_hash': hash_file(candidate / 'reviews/synthesis.json')}})
        atomic_json(candidate / 'manifest.json', manifest)
        (self.session.root / '.pending-review').symlink_to(candidate)
        store = Store.__new__(Store)
        store.root, store.path, store.manifest = self.session.root, candidate, manifest
        return store

    def test_manual_promotion_supersedes_automatic_outcome_and_preserves_history(self):
        path, before = self.trust_stop()
        store = self.replacement()
        store.promote(['synthesis'])
        store.manifest['status'] = 'complete'
        automation.manual_completion(store)
        store.save()
        current = Session({'session': str(self.session.root)})
        result = dispatch({'command': 'read', 'session': str(current.root)})['automation']
        self.assertTrue(result['superseded'])
        self.assertEqual(result['superseded_by'], current.id)
        self.assertEqual(result['outcome'], 'needs_attention')
        historical = Session({'session': str(current.root), 'generation': self.session.id})
        self.assertNotIn('superseded', automation.summary(historical))
        self.assertEqual(automation.summary(historical)['trusted_snapshot'], 'head:base')
        self.assertEqual(path.read_bytes(), before)
        # Existing completed replacements also work without the new boundary.
        store.manifest.pop('superseded_automatic_runs')
        store.save()
        self.assertTrue(automation.summary(Session({'session': str(current.root)}))['superseded'])

    def test_manual_completion_supersedes_a_pause_in_the_same_generation(self):
        path, before = self.trust_stop()
        automation.manual_completion(self.session)
        atomic_json(self.session.path / 'manifest.json', self.session.manifest)
        current = Session({'session': str(self.session.root)})
        self.assertEqual(current.id, self.session.id)
        self.assertTrue(automation.summary(current)['superseded'])
        self.assertEqual(path.read_bytes(), before)
        # A later automatic invocation remains actionable in that generation.
        operation = {**self.op, 'operation_id': 'b' * 32}
        later = automation.AutomaticRun(operation)
        token = automation._active.set(later)
        self.addCleanup(automation._active.reset, token)
        later.bind(current)
        automation.manual_completion(current)
        later.finish('failed', 'New automatic failure')
        self.assertNotIn('b' * 32, current.manifest['superseded_automatic_runs'])
        self.assertEqual(automation.summary(current)['operation_id'], 'b' * 32)
        self.assertNotIn('superseded', automation.summary(current))

    def test_only_completed_manual_analysis_retires_automatic_trust(self):
        from pr_review import pipeline, reviews
        from pr_review.sessions import Store
        self.trust_stop()
        store = Store.__new__(Store)
        store.root, store.path, store.manifest = self.session.root, self.session.path, self.session.manifest
        store.worktree, store.active, store.dual = self.f.repo, None, False
        store.configure = lambda *_: None
        store.authenticate_live = lambda *_: None
        store.snapshot = lambda pr: pr
        store.trust = lambda *_: None
        store.context = lambda *_: None
        store.session = lambda: Session({'session': str(store.root)})
        repo = SimpleNamespace(lock=self.session.pr_lock, cwd=self.f.repo, pr=7,
                               identity='github.com/org/repo', name='org/repo', host='github.com', remote='origin',
                               url=self.session.manifest['url'], pages=lambda *_: [], git=lambda *_: None,
                               metadata=lambda: {'baseRefName': 'main', 'isDraft': False})
        with patch.object(reviews.Repository, 'resolve', return_value=repo), \
                patch.object(reviews, 'locate', return_value=(store.root, self.f.repo)), \
                patch.object(reviews, 'capabilities', return_value={}), \
                patch.object(reviews, 'command', return_value=SimpleNamespace(returncode=0, stdout='reviewer')), \
                patch.object(reviews, 'Review', return_value=store), \
                patch.object(pipeline, 'synthesis'), \
                patch.object(pipeline, 'contributions', side_effect=[False, ApiError('analysis_failed', 'Failed'), True]):
            reviews.execute({'pr': 7})
            self.assertNotIn('superseded', automation.summary(store.session()))
            with self.assertRaises(ApiError):
                reviews.execute({'pr': 7})
            self.assertNotIn('superseded', automation.summary(store.session()))
            reviews.execute({'pr': 7})
        self.assertTrue(automation.summary(store.session())['superseded'])

    def test_pending_automatic_failure_remains_actionable_over_completed_review(self):
        self.trust_stop()
        store = self.replacement()
        pending = Session({'session': str(store.root),
                           'generation': automation.generation(store.path, store.manifest)})
        self.run.bind(pending)
        self.run.finish('needs_attention', 'Trust required', error_code='confirmation_required',
                        trusted_snapshot='pending:base')
        result = automation.summary(Session({'session': str(store.root)}))
        self.assertEqual(result['generation'], pending.id)
        self.assertEqual(result['trusted_snapshot'], 'pending:base')
        self.assertNotIn('superseded', result)

    def test_system_refresh_is_not_manual_intervention(self):
        state = self.saved()
        with patch.object(publication, 'repository_operation'):
            publication.refresh(self.session, state)
        self.assertIsNone(automation.manual_reason(state))

    def test_promotion_guard_preserves_manual_edits_made_during_analysis(self):
        from pr_review.sessions import Store
        from pr_review.storage import hash_file
        self.saved()
        candidate = Path(str(self.session.root) + '-history') / 'attempt-guard'
        shutil.copytree(self.session.root, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest.update(session_directory=str(self.session.root), attempt={'status': 'building'},
                        phases={'synthesis': {'status': 'completed', 'output_hash': hash_file(candidate / 'reviews/synthesis.json')}})
        atomic_json(candidate / 'manifest.json', manifest)
        (self.session.root / '.pending-review').symlink_to(candidate)
        store = Store.__new__(Store)
        store.root, store.path, store.manifest = self.session.root, candidate, manifest
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        state = self.session.load()
        mutate(self.session, state, {'operation': 'edit', 'comment': 'p1', 'body': 'Edit during analysis'})
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            store.promote(['synthesis'])
        self.assertFalse((self.session.root / '.current-review').exists())
        self.assertTrue((candidate / 'reviews/synthesis.json').exists())
        self.assertEqual(self.session.load()['comments'][0]['body'], 'Edit during analysis')

    def test_saved_trust_token_and_first_run_association(self):
        from types import SimpleNamespace
        from pr_review import models, reviews, settings
        from pr_review.repository import Repository
        repo = SimpleNamespace(common=self.f.repo / '.git', pr=7, lock=self.session.pr_lock)
        def fail(request):
            automation.observe(self.session)
            raise ApiError('confirmation_required', 'Trust required', snapshot='head:base', paths=['AGENTS.md'])
        with patch.object(Repository, 'resolve', return_value=repo), \
                patch('pr_review.sessions.locate', return_value=(None, self.f.repo)), \
                patch.object(settings, 'resolve', return_value={'settings': settings.DEFAULTS}), \
                patch.object(models, 'discover'), patch.object(models, 'validate_selection'), \
                patch.object(reviews, 'execute', side_effect=fail):
            result = automation.run({'pr': 7})
        self.assertEqual(result['exit_code'], 3)
        self.assertEqual(result['trusted_snapshot'], 'head:base')
        self.assertEqual(result['session'], str(self.session.root))
        self.assertEqual(result['agent_config_paths'], ['AGENTS.md'])
        self.assertTrue(Path(result['log_path']).exists())

    def test_noop_still_checks_freshness(self):
        self.saved(lambda s: s.update(comments=[]))
        def validate(session, operation, verdict='COMMENT'):
            if operation == 'validate':
                raise ApiError('operation_failed', 'PR head changed')
        with patch.object(publication, 'repository_operation', side_effect=validate), patch.object(publication, 'gh') as gh:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        gh.assert_not_called()

    def test_noop_does_not_ignore_discussion_changes_without_staged_drafts(self):
        self.saved(lambda s: s.update(comments=[]))
        def refresh(session, state):
            state['discussion_hash'] = 'changed-after-analysis'
            return []
        with patch.object(publication, 'refresh', side_effect=refresh):
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)

    def test_final_publication_check_does_not_resend_a_late_failed_batch(self):
        state = self.saved()
        state['batches'] = [{'actions': [{'status': 'draft'}]}]
        self.session.save(state)
        with patch.object(publication, 'publish') as publish:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        publish.assert_not_called()

    def test_newer_pending_analysis_blocks_publication_of_previous_completed_result(self):
        candidate = Path(str(self.session.root) + '-history') / 'attempt-pending'
        shutil.copytree(self.session.root, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest.update(session_directory=str(self.session.root), status='paused')
        atomic_json(candidate / 'manifest.json', manifest)
        (self.session.root / '.pending-review').symlink_to(candidate)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(posts)
