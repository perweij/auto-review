"""Issue writes retain shared journals while adapters own remote semantics."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_issues
from pr_review.common import ApiError
from pr_review.issue_writes import Writes


class WriteAdapterTest(unittest.TestCase):
    def fixture(self):
        adapter = Mock(capabilities={'comment', 'create', 'draft'})
        repo = SimpleNamespace(forge=SimpleNamespace(issue_writes=adapter))
        state = {'actions': {}, 'remote_head': 'head'}
        writes = Writes(repo, state, Mock(), Mock())
        return writes, adapter, state

    def test_lost_draft_response_is_confirmed_without_retry(self):
        writes, adapter, state = self.fixture()
        adapter.draft_state.side_effect = [{'id': 'opaque', 'draft': True}, {'id': 'opaque', 'draft': False}]
        adapter.set_draft.side_effect = ApiError('network', 'Lost response')
        writes.draft(False)
        adapter.set_draft.assert_called_once_with(writes, 'opaque', False)
        self.assertEqual(state['actions']['draft:False:head']['status'], 'complete')

    def test_already_satisfied_draft_reconciles_saved_intent(self):
        writes, adapter, state = self.fixture()
        state['actions']['draft:False:head'] = {'status': 'submitting'}
        adapter.draft_state.return_value = {'id': 'opaque', 'draft': False}
        writes.draft(False)
        adapter.set_draft.assert_not_called()
        self.assertEqual(state['actions']['draft:False:head']['status'], 'complete')

    def test_cancelled_transition_retains_intent(self):
        writes, adapter, state = self.fixture()
        adapter.draft_state.return_value = {'id': 'opaque', 'draft': True}
        adapter.set_draft.side_effect = ApiError('cancelled', 'Stopped')
        with self.assertRaises(ApiError) as caught:
            writes.draft(False)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(state['actions']['draft:False:head']['status'], 'submitting')

    def test_unsupported_capabilities_fail_before_mutation(self):
        writes, adapter, state = self.fixture()
        adapter.capabilities = {'comment'}
        with self.assertRaises(ApiError):
            writes.ensure_supported()
        self.assertEqual(state['actions'], {})
        adapter.set_draft.assert_not_called()

    def test_readback_rejection_does_not_reset_submitted_comment(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        with patch('pr_review.forge_github_issue_writes.pages', side_effect=[[], ApiError('command_failed', 'HTTP 403')]):
            with self.assertRaises(ApiError) as caught:
                fixture.send()
        self.assertEqual(caught.exception.code, 'uncertain')
        self.assertEqual(fixture.state['actions'][fixture.key]['status'], 'submitting')
        fixture.writes.api.assert_called_once()

    def test_account_check_blocks_readback_and_send(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        fixture.writes.account_check = Mock(side_effect=ApiError('account_changed', 'Changed'))
        with self.assertRaises(ApiError) as caught:
            fixture.send()
        self.assertEqual(caught.exception.code, 'account_changed')
        fixture.writes.api.assert_not_called()

    def test_garbled_draft_response_still_reads_back(self):
        writes, adapter, state = self.fixture()
        adapter.draft_state.side_effect = [{'id': 'opaque', 'draft': True}, {'id': 'opaque', 'draft': False}]
        adapter.set_draft.side_effect = ValueError('Truncated response')
        writes.draft(False)
        self.assertEqual(state['actions']['draft:False:head']['status'], 'complete')

    def test_satisfied_transition_reconciles_old_head(self):
        writes, adapter, state = self.fixture()
        state['actions']['draft:False:old-head'] = {'status': 'submitting'}
        adapter.draft_state.return_value = {'id': 'opaque', 'draft': False}
        writes.draft(False)
        self.assertEqual(state['actions']['draft:False:old-head']['status'], 'complete')

    def test_deleted_author_on_unrelated_comment_does_not_block(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        fixture.remote.append({'id': 999, 'body': None, 'user': None})
        with patch('pr_review.forge_github_issue_writes.pages', side_effect=fixture.page):
            fixture.send()
        self.assertEqual(fixture.state['actions'][fixture.key]['status'], 'complete')

    def test_malformed_saved_receipt_is_inspectable_as_changed(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        action = {'status': 'complete', 'body': 'Hello', 'receipt': 'bad'}
        self.assertEqual(fixture.writes.readback(fixture.key, action)['outcome'], 'changed')

    def test_resource_limit_readback_is_not_publication_uncertainty(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        with patch('pr_review.forge_github_issue_writes.pages', side_effect=ApiError('resource_limit', 'Budget')):
            with self.assertRaises(ApiError) as caught:
                fixture.send()
        self.assertEqual(caught.exception.code, 'resource_limit')
        fixture.writes.api.assert_not_called()

    def test_empty_creation_body_is_inspectable_as_changed(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('pr')
        with patch('pr_review.forge_github_issue_writes.pages', side_effect=fixture.page):
            fixture.send()
            fixture.remote[0]['body'] = None
            self.assertEqual(fixture.writes.recover()[fixture.key]['readback']['outcome'], 'changed')

    def test_post_send_account_lookup_failure_is_uncertain(self):
        fixture = test_issues.WriteRecoveryTest()
        fixture.make_writes('comment')
        fixture.writes.account_check = Mock(side_effect=[None, None, ApiError('command_failed', 'Lookup failed')])
        with patch('pr_review.forge_github_issue_writes.pages', side_effect=fixture.page):
            with self.assertRaises(ApiError) as caught:
                fixture.send()
        self.assertEqual(caught.exception.code, 'uncertain')
        self.assertEqual(fixture.state['actions'][fixture.key]['status'], 'submitting')
        fixture.writes.api.assert_called_once()

    def test_guard_incomplete_facts_is_not_account_change(self):
        from pr_review.issues import Workflow
        flow = SimpleNamespace(state={'issue': 12, 'viewer': 'author'}, repo=Mock(), file=Mock(), request={})
        flow.file.exists.return_value = False
        with patch('pr_review.issues.collect_eligibility_facts', return_value={'complete': False, 'error': 'Offline'}):
            with self.assertRaises(ApiError) as caught:
                Workflow.guard(flow)
        self.assertEqual(caught.exception.code, 'preflight_failed')

    def test_malformed_saved_receipt_cannot_crash_input_hashing(self):
        from pr_review.issues import semantic_comments
        with self.assertRaises(ApiError) as caught:
            semantic_comments([], {'actions': {'old': {'body': 'Text', 'receipt': 'bad'}}})
        self.assertEqual(caught.exception.code, 'invalid_session')

    def test_successful_transition_reconciles_old_heads(self):
        writes, adapter, state = self.fixture()
        state['actions']['draft:False:old'] = {'status': 'submitting'}
        state['actions']['draft:True:old'] = {'status': 'submitting'}
        adapter.draft_state.side_effect = [{'id': 'opaque', 'draft': True}, {'id': 'opaque', 'draft': False}]
        writes.draft(False)
        self.assertEqual(state['actions']['draft:False:old']['status'], 'complete')
        self.assertEqual(state['actions']['draft:True:old']['status'], 'superseded')

    def test_deleted_author_survives_issue_reads_without_authorizing_approval(self):
        from pr_review.forge_github_issues import GitHubIssues
        from pr_review.issues import semantic_comments, approval_revision
        repo = SimpleNamespace(identity='github.com/org/repo', name='org/repo', cwd='.')
        comment = {'id': 99, 'body': '/fix-issue approve 1', 'user': None}
        with patch('pr_review.forge_github_issues.github_json', return_value=[[comment]]):
            comments = GitHubIssues().comments(repo, 12)
        state = {'viewer': 'author', 'actions': {}}
        self.assertIsNone(approval_revision(comments[0], state))
        self.assertEqual(semantic_comments(comments, state)[0]['body'], comment['body'])
