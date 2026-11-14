import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from pr_review import issue_signals
from pr_review.common import ApiError
from pr_review.issue_writes import Writes


class SignalsTest(unittest.TestCase):
    def flow(self):
        comment = {'id': 1, 'body': '/fix-issue approve 1', 'user': {'login': 'alice'}, 'created_at': '2026-01-01'}
        from pr_review.forge_github_issue_writes import GitHubIssueWrites
        writes = Mock()
        writes.adapter = GitHubIssueWrites()
        writes.repo = SimpleNamespace(name='a/b')
        return SimpleNamespace(state={'viewer': 'alice', 'actions': {}, 'phase': 'waiting', 'waiting': 'approval',
                                     'plan': {'revision': 2, 'published_at': '2026-02-01'}},
                               facts={'comments': [comment]}, repo=SimpleNamespace(name='a/b'),
                               writes=writes, save=Mock())

    def test_acknowledgment_retries_then_retains_receipt(self):
        f = self.flow()
        f.writes.api.side_effect = [ApiError('command_failed', 'HTTP 403'), {'id': 2, 'content': 'eyes'}]
        issue_signals.acknowledge(f)
        issue_signals.acknowledge(f)
        issue_signals.acknowledge(f)
        self.assertEqual(2, f.writes.api.call_count)
        self.assertNotIn('error', f.state['command_signals']['1'])

    def test_filters_authorship_quotes_and_receipted_commands(self):
        for body, author in [('> /fix-issue approve 1', 'alice'), ('/fix-issue approve 1', 'bob'), ('hello', 'alice')]:
            f = self.flow()
            f.facts['comments'][0].update(body=body, user={'login': author})
            issue_signals.acknowledge(f)
            f.writes.api.assert_not_called()
        f = self.flow()
        f.state['actions']['approve:1'] = {'receipt': f.facts['comments'][0]}
        issue_signals.acknowledge(f)
        f.writes.api.assert_not_called()

    def test_cancellation_propagates(self):
        f = self.flow()
        f.writes.api.side_effect = ApiError('cancelled', 'cancel')
        with self.assertRaises(ApiError): issue_signals.acknowledge(f)

    def test_rejection_key_stable_and_accepted_commands_not_rejected_later(self):
        f = self.flow()
        issue_signals.explain(f)
        first = f.writes.comment.call_args
        issue_signals.explain(f)
        self.assertEqual(first, f.writes.comment.call_args)
        self.assertIn('approve 2', first.args[0])
        f.facts['comments'][0].update(body='/fix-issue approve 2', created_at='2026-03-01')
        f.writes.comment.reset_mock()
        issue_signals.explain(f)
        f.state['plan']['revision'] = 3
        issue_signals.explain(f)
        f.writes.comment.assert_not_called()

    def test_attention_preserves_existing_intents_and_detects_changes(self):
        state = {'viewer': 'alice', 'actions': {}}
        writes = Writes(None, state, Mock(), Mock())
        self.assertEqual('@alice\n\nQuestion', writes.attention_body('Question', 'questions:1'))
        self.assertEqual('Done', writes.attention_body('Done', 'complete:1'))
        state['actions']['questions:1'] = {'body': 'Question'}
        self.assertEqual('Question', writes.attention_body('Question', 'questions:1'))
        with self.assertRaises(ApiError): writes.attention_body('Different', 'questions:1')


class WorkflowSignalsTest(unittest.TestCase):
    def setUp(self):
        import test_issues
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_stale_command_is_acknowledged_and_explained_once_without_replanning(self):
        from pr_review.issues import dispatch
        from unittest.mock import patch
        request = self.t.terminal_request()
        self.assertEqual('approval', dispatch(request)['outcome'])
        initial = len(self.t.assessments)
        self.t.f['comments'].append({'id': 900, 'body': '/fix-issue approve 99',
                                    'user': {'login': 'author'}, 'created_at': '2026-11-10'})
        original = self.t.api
        with patch('pr_review.issue_writes.Writes.api', side_effect=original) as api:
            self.assertEqual('approval', dispatch(request)['outcome'])
            self.assertEqual('approval', dispatch(request)['outcome'])
        reactions = [c for c in api.call_args_list if c.args[0].endswith('/reactions')]
        self.assertEqual(1, len(reactions))
        explanations = [c for c in self.t.f['comments'] if 'cannot be accepted' in c['body']]
        self.assertEqual(1, len(explanations))
        self.assertTrue(explanations[0]['body'].startswith('@author\n\n'))
        self.assertEqual(initial, len(self.t.assessments))

    def test_acknowledgment_failure_does_not_block_approval_wait(self):
        from pr_review.issues import dispatch
        from unittest.mock import patch
        request = self.t.terminal_request()
        self.assertEqual('approval', dispatch(request)['outcome'])
        self.t.f['comments'].append({'id': 901, 'body': '/fix-issue approve 9',
                                    'user': {'login': 'author'}, 'created_at': '2026-11-10'})
        original = self.t.api
        def unavailable(endpoint, payload, **kwargs):
            if endpoint.endswith('/reactions'):
                raise ApiError('command_failed', 'HTTP 403')
            return original(endpoint, payload, **kwargs)
        with patch('pr_review.issue_writes.Writes.api', side_effect=unavailable):
            self.assertEqual('approval', dispatch(request)['outcome'])
        self.assertTrue(any('cannot be accepted' in c['body'] for c in self.t.f['comments']))
