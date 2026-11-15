import json
import tempfile
import unittest
from pathlib import Path
from pr_review.session_report import generate, render


class ReportTest(unittest.TestCase):
    def test_offline_report_excludes_gaps_and_preserves_unknown_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'session.json').write_text(json.dumps({'issue': 39, 'assessment': {}, 'phase': 'ready'}))
            (root / 'usage').mkdir()
            calls = {'a': {'id': 'a', 'role': 'fix', 'duration_seconds': 60,
                           'started_at': '2026-01-01', 'usage': {'total_tokens': 100}},
                     'b': {'id': 'b', 'role': 'final-review/reviewer-1', 'duration_seconds': 120,
                           'started_at': '2026-01-03', 'model': '<script>|bad'}}
            (root / 'usage/ledger.json').write_text(json.dumps({'calls': calls}))
            fields, body = generate(root)
            data = json.loads(Path(fields['report_data_path']).read_text())
            self.assertEqual(180, sum(r['seconds'] for r in data['activities']))
            self.assertIsNone(data['activities'][1]['tokens'])
            self.assertNotIn('<script>', body)
            self.assertIn('&#124;', body)
            self.assertIn('3.0 minutes', body)
            self.assertIn('pie', body)

    def test_wrong_directory_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                generate(directory)
            self.assertFalse((Path(directory) / 'reports').exists())

    def test_empty_report_has_no_pie(self):
        self.assertNotIn('\npie\n', render({'activities': [], 'outcome': 'unknown'}))

    def test_trace_persists_reuse_and_excludes_inactive_gaps(self):
        from pr_review import session_trace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(session_trace.time, 'monotonic', side_effect=[10, 11, 15]):
                with session_trace.operation(directory, True):
                    session_trace.reuse('planning-draft')
            data = json.loads((Path(directory) / 'execution-trace.json').read_text())
            self.assertEqual(5, data['invocations'][0]['seconds'])
            self.assertEqual('reused', data['executions'][0]['status'])
            self.assertEqual(0, data['executions'][0]['seconds'])

    def test_publication_failure_does_not_change_task_result(self):
        from pr_review.session_report import finish
        from unittest.mock import Mock, patch
        flow = Mock()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        flow.path = temporary.name
        flow.state = {'session_report': True, 'phase': 'ready'}
        flow.writes.comment.side_effect = ValueError('lost response')
        with patch('pr_review.session_report.generate', return_value=({'report_path': '/local'}, 'body')):
            result = finish(flow, {'outcome': 'ready', 'exit_code': 0})
        self.assertEqual(0, result['exit_code'])
        self.assertEqual('ready', result['outcome'])
        self.assertEqual('incomplete', result['report_publication'])
        flow.writes.comment.assert_called_once()

    def test_issue_workflow_opt_in_persists_and_generates_at_approval(self):
        import test_issues
        from pr_review.issues import dispatch
        fixture = test_issues.IssueIntegrationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = dispatch(fixture.terminal_request('--session-report'))
        self.assertEqual('approval', result['outcome'])
        self.assertEqual('local-only', result['report_publication'])
        self.assertTrue(Path(result['report_path']).is_file())
        state = json.loads((Path(result['session']) / 'session.json').read_text())
        self.assertTrue(state['session_report'])
        result = dispatch(fixture.terminal_request('--no-session-report'))
        self.assertNotIn('report_path', result)
        state = json.loads((Path(result['session']) / 'session.json').read_text())
        self.assertFalse(state['session_report'])

    def test_completion_resume_uses_frozen_publication_body(self):
        from pr_review.session_report import finish
        from unittest.mock import Mock, patch
        from pr_review.common import digest
        with tempfile.TemporaryDirectory() as directory:
            flow = Mock(path=directory)
            key = 'session-report:' + digest([None, None])
            flow.state = {'session_report': True, 'phase': 'ready', 'actions': {
                key: {'body': 'frozen', 'source_body': 'frozen', 'status': 'submitting'}}}
            with patch('pr_review.session_report.generate', return_value=({}, 'changed')):
                result = finish(flow, {'outcome': 'ready', 'exit_code': 0})
            flow.writes.comment.assert_called_once_with('frozen', key)
            self.assertEqual('complete', result['report_publication'])

    def test_bounded_report_remains_below_comment_limit(self):
        row = dict(category='Implementation', role='fix', provider='codex', model='x'*500,
                   effort='high', seconds=1, tokens=None, status='completed')
        body = render({'activities': [row]*500, 'outcome': 'ready',
                       'outcomes': [{'stage': 'Implementation', 'summary': 'x'*5000}]*100}, bounded=True)
        self.assertLess(len(body), 30000)
        self.assertIn('omitted', body)

    def test_corrupt_trace_does_not_block_work(self):
        from pr_review import session_trace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'execution-trace.json'
            path.write_text('{broken')
            with session_trace.operation(directory, True):
                session_trace.reuse('planning-draft')
            self.assertEqual('{broken', path.read_text())

    def test_same_second_calls_preserve_recorded_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'session.json').write_text(json.dumps({'kind': 'issue', 'phase': 'ready'}))
            (root / 'usage').mkdir()
            calls = {key: dict(id=key, started_at='2026-01-01T00:00:00+00:00') for key in ('z', 'a')}
            (root / 'usage/ledger.json').write_text(json.dumps({'calls': calls}))
            fields, _ = generate(root)
            data = json.loads(Path(fields['report_data_path']).read_text())
            self.assertEqual(['z', 'a'], [r['id'] for r in data['activities']])
