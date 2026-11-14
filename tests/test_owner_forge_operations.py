"""Owner operation identities, recovery and shared conversation read-back."""
import copy
import unittest
from unittest.mock import patch

import test_owner as fixtures
from pr_review.common import ApiError
from pr_review.owner_state import OwnerSession


class OwnerOperationsTest(unittest.TestCase):
    def setUp(self):
        self.t = fixtures.OwnerTest()
        self.addCleanup(self.t.doCleanups)
        self.t.setUp()

    def prepare(self):
        self.t.request('owner-mutate', operation='reply', item='thread:T1', body='Original')
        self.reply = self.t.view['replies'][-1]['id']
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        return self.t.request('owner-prepare')

    def session_state(self):
        session = OwnerSession({'session': self.t.path})
        return session, session.load()

    def test_repeated_preview_preserves_operation_and_token(self):
        first = self.prepare()
        second = self.t.request('owner-prepare')
        self.assertEqual(first['token'], second['token'])
        session, state = self.session_state()
        self.assertEqual(state['prepared']['operations'][0]['body'], 'Original')
        self.assertFalse(self.t.posts)

    def test_legacy_preview_requires_new_confirmation(self):
        prepared = self.prepare()
        session, state = self.session_state()
        state['prepared'].pop('operations')
        session.save(state)
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-publish', token=prepared['token'])
        self.assertEqual(caught.exception.code, 'stale_confirmation')
        self.assertFalse(self.t.posts)

    def failed_send(self):
        prepared = self.prepare()
        with patch('pr_review.publication.gh', side_effect=ApiError('network', 'Offline')):
            with self.assertRaises(ApiError):
                self.t.request('owner-publish', token=prepared['token'])
        self.t.request('owner-recover')
        return copy.deepcopy(self.session_state()[1]['actions'][0])

    def test_retry_preserves_marker(self):
        action = self.failed_send()
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        prepared = self.t.request('owner-prepare')
        self.t.request('owner-publish', token=prepared['token'])
        state = self.session_state()[1]
        self.assertEqual(len(state['actions']), 1)
        self.assertEqual(state['actions'][0]['marker'], action['marker'])
        self.assertEqual(len(self.t.posts), 1)

    def test_late_legacy_receipt_does_not_certify_edited_draft(self):
        action = self.failed_send()
        session, state = self.session_state()
        state['actions'][0].pop('body')
        session.save(state)
        self.t.request('owner-mutate', operation='edit', reply=self.reply, body='Changed')
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        self.t.publish(session, 'comments/101/replies', action['payload'])
        with self.assertRaises(ApiError):
            self.t.request('owner-prepare')
        state = self.session_state()[1]
        self.assertEqual(state['actions'][0]['status'], 'published')
        self.assertEqual(state['replies'][0]['status'], 'unstaged')
        self.assertEqual(state['replies'][0]['body'], 'Changed')
        self.assertTrue(state['replies'][0]['historical_receipts'])
        self.assertEqual(len(self.t.posts), 1)

    def test_known_receipt_absence_does_not_allow_retry(self):
        action = self.failed_send()
        session, state = self.session_state()
        state['actions'][0].update(status='ambiguous', receipt={'id': 123})
        session.save(state)
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-recover')
        self.assertEqual(caught.exception.code, 'ambiguous_receipt')
        self.assertEqual(self.session_state()[1]['actions'][0]['status'], 'ambiguous')

    def test_satisfied_thread_does_not_send(self):
        self.t.feedback[0]['is_resolved'] = True
        with patch('pr_review.forge_github_conversations.GitHubConversations.set_state') as send:
            view = self.t.request('owner-thread-state', item='thread:T1', resolved=True)
        send.assert_not_called()
        self.assertTrue(view['feedback'][0]['is_resolved'])

    def test_cancelled_thread_retains_intent_and_refresh_reconciles(self):
        def cancelled(*args):
            self.t.feedback[0]['is_resolved'] = True
            raise ApiError('cancelled', 'Stopped')
        with patch('pr_review.forge_github_conversations.GitHubConversations.set_state', side_effect=cancelled) as send:
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-thread-state', item='thread:T1', resolved=True)
        self.assertEqual(caught.exception.code, 'cancelled')
        send.assert_called_once()
        self.assertEqual(self.session_state()[1]['thread_intent']['status'], 'ambiguous')
        self.t.request('owner-refresh')
        state = self.session_state()[1]
        self.assertNotIn('thread_intent', state)
        self.assertEqual(state['thread_history'][-1]['status'], 'confirmed')

    def test_unconfirmed_thread_is_not_retried_on_refresh(self):
        with patch('pr_review.forge_github_conversations.GitHubConversations.set_state') as send:
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-thread-state', item='thread:T1', resolved=True)
            self.t.request('owner-refresh')
        self.assertEqual(caught.exception.code, 'uncertain_thread_state')
        send.assert_called_once()
        self.assertIn('thread_intent', self.session_state()[1])

    def test_uncertain_actions_on_different_threads_remain_pending(self):
        from pr_review.conversations import pending
        with patch('pr_review.forge_github_conversations.GitHubConversations.set_state') as send:
            for key in ('thread:T1', 'thread:T2'):
                with self.assertRaises(ApiError):
                    self.t.request('owner-thread-state', item=key, resolved=True)
        self.assertEqual(send.call_count, 2)
        self.assertEqual({i['item'] for i in pending(self.session_state()[1])}, {'thread:T1', 'thread:T2'})
        self.t.feedback[0]['is_resolved'] = True
        self.t.request('owner-refresh')
        self.assertEqual([i['item'] for i in pending(self.session_state()[1])], ['thread:T2'])

    def test_late_legacy_receipt_preserves_already_published_reply(self):
        self.late_published_receipt('Changed')

    def test_late_legacy_receipt_with_same_wording_preserves_current_receipt(self):
        self.late_published_receipt('Original')

    def late_published_receipt(self, body):
        action = self.failed_send()
        session, state = self.session_state()
        state['actions'][0].pop('body')
        session.save(state)
        self.t.request('owner-mutate', operation='edit', reply=self.reply, body=body)
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        prepared = self.t.request('owner-prepare')
        self.t.request('owner-publish', token=prepared['token'])
        before = copy.deepcopy(self.session_state()[1]['replies'][0])
        session, state = self.session_state()
        state['actions'][0].pop('body', None)
        session.save(state)
        self.t.publish(session, 'comments/101/replies', action['payload'])
        with self.assertRaises(ApiError):
            self.t.request('owner-prepare')
        after = self.session_state()[1]['replies'][0]
        self.assertEqual(after['status'], 'published')
        self.assertEqual(after['receipt'], before['receipt'])
        self.assertEqual(after['evidence'], before['evidence'])
        self.assertEqual(len(after['historical_receipts']), 1)
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-mutate', operation='stage', reply=self.reply)
        self.assertEqual(caught.exception.code, 'immutable_reply')

    def test_recovery_records_receipt_before_refreshing_other_drafts(self):
        action = self.failed_send()
        session, state = self.session_state()
        state['actions'][0]['status'] = 'ambiguous'
        session.save(state)
        self.t.request('owner-mutate', operation='reply', item='thread:T1', body='Another reply')
        other = self.t.view['replies'][-1]['id']
        self.t.request('owner-mutate', operation='stage', reply=other)
        receipt = self.t.publish(session, 'comments/101/replies', action['payload'])
        self.t.feedback[0]['comments'].append(dict(id=receipt['id'], author='author', body=receipt['body'],
                                                  url=receipt['html_url'], created_at='today', updated_at='today'))
        self.t.request('owner-recover')
        state = self.session_state()[1]
        self.assertEqual(state['replies'][0]['status'], 'published')
        self.assertNotIn('needs_attention', state['replies'][0])
        self.assertEqual(state['replies'][1]['status'], 'staged')
        self.assertEqual(state['replies'][0]['fingerprint'], state['feedback'][0]['fingerprint'])

    def test_explicit_satisfied_request_retires_legacy_opposite_intent(self):
        from pr_review.conversations import pending
        self.t.feedback[0]['is_resolved'] = True
        self.t.request('owner-refresh')
        session, state = self.session_state()
        state['thread_intent'] = dict(item='thread:T1', resolved=False)
        session.save(state)
        with patch('pr_review.forge_github_conversations.GitHubConversations.set_state') as send:
            self.t.request('owner-thread-state', item='thread:T1', resolved=True)
        send.assert_not_called()
        state = self.session_state()[1]
        self.assertFalse(pending(state))
        self.assertEqual(state['thread_history'][-1]['status'], 'superseded')

    def test_saved_receipt_is_recovered_after_crash_before_finish(self):
        action = self.failed_send()
        session, state = self.session_state()
        receipt = self.t.publish(session, 'comments/101/replies', action['payload'])
        state['actions'][0].update(status='submitting', receipt=receipt)
        session.save(state)
        self.t.request('owner-recover')
        state = self.session_state()[1]
        self.assertEqual(state['replies'][0]['status'], 'published')
        self.assertEqual(state['replies'][0]['receipt'], receipt)
        self.assertEqual(len(self.t.posts), 1)

    def test_timeline_receipt_lookup_uses_issue_comments(self):
        from pr_review.forge_github_publication import GitHubPublication
        session, state = self.session_state()
        action = dict(kind='timeline', version=1, marker='marker', viewer='author')
        with patch('pr_review.publication.gh', return_value=[]) as transport:
            self.assertEqual(GitHubPublication().lookup(session, action), [])
        transport.assert_called_once_with(session, 'comments', pages=True, resource='issues')

    def test_late_duplicate_of_retried_operation_is_reported(self):
        action = self.failed_send()
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        prepared = self.t.request('owner-prepare')
        self.t.request('owner-publish', token=prepared['token'])
        session, state = self.session_state()
        self.assertEqual(state['actions'][0]['attempt_count'], 2)
        self.t.publish(session, 'comments/101/replies', action['payload'])
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-recover')
        self.assertEqual(caught.exception.code, 'ambiguous_receipt')
        reply = self.session_state()[1]['replies'][0]
        self.assertEqual(reply['status'], 'published')
        self.assertIn('Multiple remote receipts', reply['needs_attention'])
        self.assertEqual(len(self.t.posts), 2)

    def test_same_receipt_with_different_metadata_is_not_a_duplicate(self):
        action = self.failed_send()
        self.t.request('owner-mutate', operation='stage', reply=self.reply)
        prepared = self.t.request('owner-prepare')
        self.t.request('owner-publish', token=prepared['token'])
        self.t.receipts[0]['reactions'] = {'total_count': 1}
        self.t.request('owner-recover')
        reply = self.session_state()[1]['replies'][0]
        self.assertNotIn('historical_receipts', reply)
        self.assertNotIn('needs_attention', reply)
        self.assertNotIn('reactions', reply['receipt'])

    def test_owner_receipt_scan_lists_each_resource_once(self):
        from pr_review.forge_github_publication import GitHubPublication
        session, state = self.session_state()
        actions = [dict(id=str(i), kind=kind, version=1, marker='marker', viewer='author')
                   for i, kind in enumerate(('reply', 'reply', 'timeline', 'timeline'))]
        with patch('pr_review.publication.gh', return_value=[]) as transport:
            result = GitHubPublication().lookup_owner(session, actions)
        self.assertEqual(result, {str(i): [] for i in range(4)})
        self.assertEqual(transport.call_count, 2)
