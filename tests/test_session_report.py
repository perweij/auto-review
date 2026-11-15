import json
import tempfile
import unittest
from pathlib import Path
from pr_review.session_report import generate, render


class ReportTest(unittest.TestCase):
    def test_historical_session_is_unsupported_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.json'
            path.write_text(json.dumps({'kind': 'issue', 'phase': 'ready'}))
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'new issue sessions'):
                generate(directory)
            self.assertEqual(before, path.read_bytes())
            self.assertFalse((Path(directory) / 'reports').exists())

    def test_report_renders_explicit_edges_and_excludes_waits(self):
        from pr_review import session_trace as trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'session.json').write_text(json.dumps({'kind': 'issue', 'id': 'id', 'execution_trace_version': 2, 'phase': 'ready'}))
            trace.initialize(root, 'id')
            with trace.operation(root, True, 'id'):
                with trace.stage('planning', choice={'model': '<bad>|model'}) as handle:
                    first = trace.complete(handle, {'decision': 'plan', 'summary': 'Plan is ready'})
                trace.decision('approved', sources=[first])
            fields, body = generate(root)
            data = json.loads(Path(fields['report_data_path']).read_text())
            self.assertEqual(1, len(data['transitions']))
            self.assertIn('Plan is ready', body)
            self.assertNotIn('<bad>', body)
            self.assertIn('approved', body)
            self.assertNotIn('historical', body.lower())

    def test_shared_ledger_isolates_batch_usage_and_coverage(self):
        from pr_review import session_trace as trace
        from pr_review.session_report import report_data
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace.initialize(root, 'batch')
            with trace.operation(root, True, 'batch'):
                with trace.stage('implementation') as handle:
                    handle[1]['call_ids'] = ['own-call']
                    trace.complete(handle, {'status': 'ready'})
            journal = json.loads((root / 'execution-trace.json').read_text())
            calls = {'own-call': {'usage': {'total_tokens': 12}, 'coverage': 'reported'},
                     'another-batch': {'usage': {'total_tokens': 900}, 'coverage': 'reported'}}
            data = report_data(journal, calls, shared_ledger=True)
            self.assertEqual('complete', data['coverage'])
            self.assertEqual(1, data['total_calls'])
            self.assertEqual(12, sum(row['tokens'] or 0 for row in data['activities']))
            self.assertEqual('partial', report_data(journal, calls)['coverage'])
            del calls['own-call']
            missing = report_data(journal, calls, shared_ledger=True)
            self.assertEqual('partial', missing['coverage'])
            self.assertEqual(0, missing['reported_calls'])

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
        result = dispatch(fixture.terminal_request())
        trace_path = Path(result['session']) / 'execution-trace.json'
        self.assertEqual(2, json.loads(trace_path.read_text())['version'])
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
        rows = [dict(id=str(i), sequence=i, stage=True, context={}, category='Implementation', role='implementation',
                     provider='codex', model='x'*500, effort='high', seconds=1, tokens=None,
                     status='ready', summary='x'*4000) for i in range(500)]
        body = render(dict(activities=rows, outcome='ready', transitions=[], coverage='partial',
                           reported_calls=0, total_calls=500, repairs=0, stage_numbers={}), bounded=True)
        self.assertLess(len(body), 30000)
        self.assertIn('omitted', body)
