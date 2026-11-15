"""Semantic traces preserve explicit dependencies without controlling workflows."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pr_review import session_trace as trace
from pr_review.common import ApiError


class SemanticTraceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        trace.initialize(self.root, 'session')

    def data(self):
        return json.loads((self.root / 'execution-trace.json').read_text())

    def result(self, role, name, inputs=(), choice=None, value=None):
        artifact = self.root / name
        with trace.stage(role, inputs=inputs, choice=choice, artifact=artifact) as handle:
            artifact.write_text(name)
            return trace.complete(handle, value or {'status': 'evidence_available'}, artifact)

    def test_synthesis_consumes_both_reviewers_without_order_edge(self):
        with trace.operation(self.root, True, 'session'):
            first = self.result('reviewer-1', 'one')
            second = self.result('reviewer-2', 'two')
            result = self.result('acceptance', 'three', [first, second], value={'assessment': 'changes_requested'})
            decision = trace.decision('repair_allocated', sources=[result], key='cycle-1')
            self.assertEqual(decision, trace.decision('repair_allocated', sources=[result], key='cycle-1'))
        data = self.data()
        self.assertEqual(4, len(data['stages']))
        self.assertEqual([], data['stages'][1]['inputs'])
        self.assertEqual([first, second], data['stages'][2]['inputs'])
        self.assertEqual('changes_requested', data['stages'][2]['outcome'])
        self.assertEqual(3, len(data['transitions']))

    def test_process_completion_does_not_validate_stage(self):
        with trace.operation(self.root, True, 'session'):
            with self.assertRaises(ApiError):
                with trace.stage('planning', artifact=self.root / 'result'):
                    raise ApiError('invalid_result', 'Invalid completion')
        self.assertEqual('failed', self.data()['stages'][0]['status'])
        self.assertEqual('invalid_result', self.data()['stages'][0]['outcome'])

    def test_resume_reuse_retains_configuration_and_no_new_usage(self):
        with trace.operation(self.root, True, 'session'):
            original = self.result('planning', 'result', choice={'model': 'original'})
        with trace.operation(self.root, True, 'session'):
            trace.reuse('planning', self.root / 'result', {'decision': 'plan'}, choice={'model': 'replacement'})
        reused = self.data()['stages'][1]
        self.assertEqual(original, reused['reused_from'])
        self.assertEqual({'model': 'original'}, reused['choice'])
        self.assertEqual([], reused['call_ids'])
        self.assertEqual(0, reused['seconds'])

    def test_interrupted_stage_requires_exact_validated_artifact_for_recovery(self):
        path = self.root / 'result'
        with trace.operation(self.root, True, 'session'):
            with trace.stage('planning', artifact=path) as handle:
                path.write_text('validated by workflow later')
                original = handle[1]['id']
        with trace.operation(self.root, True, 'session'):
            trace.reuse('planning', path, {'decision': 'plan'})
        first, reused = self.data()['stages']
        self.assertTrue(first['recovered_result'])
        self.assertEqual('unknown', first['timing_coverage'])
        self.assertEqual(original, reused['reused_from'])

    def test_missing_trace_is_not_recreated(self):
        (self.root / 'execution-trace.json').unlink()
        with trace.operation(self.root, True, 'session'):
            self.assertIsNone(trace.start('planning'))
        self.assertFalse((self.root / 'execution-trace.json').exists())

    def test_write_failure_does_not_stop_work(self):
        with patch.object(trace, 'atomic_json', side_effect=OSError('disk unavailable')):
            with trace.operation(self.root, True, 'session'):
                with trace.stage('planning') as handle:
                    trace.complete(handle, {'decision': 'plan'})

    def test_wait_accounting_is_independent_of_reporting(self):
        with patch.object(trace, 'now', return_value='2026-01-01T00:00:00+00:00'):
            trace.boundary(self.root, 'session', {'outcome': 'approval', 'next_action': 'approve'})
        with patch.object(trace, 'now', return_value='2026-01-01T01:00:00+00:00'):
            with trace.operation(self.root, True, 'session'):
                pass
        self.assertEqual(3600, self.data()['waiting_seconds'])
        self.assertLess(self.data()['invocations'][0]['seconds'], 1)

    def test_foreign_session_trace_is_rejected(self):
        with trace.operation(self.root, True, 'other'):
            self.assertIsNone(trace.start('planning'))
        self.assertEqual([], self.data()['stages'])

    def test_verification_recovery_records_controller_evidence_edges(self):
        from test_issue_verification import VerificationRecoveryTest
        fixture = VerificationRecoveryTest('test_missing_source_identity_collects_candidate_evidence_and_resumes_synthesis')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with trace.operation(self.root, True, 'session'):
            fixture.test_missing_source_identity_collects_candidate_evidence_and_resumes_synthesis()
        data = self.data()
        verification = [s for s in data['stages'] if s['role'] == 'verification']
        self.assertEqual(1, len(verification))
        self.assertEqual('completed', verification[0]['outcome'])
        later = [s for s in data['stages'] if s['role'] == 'acceptance' and verification[0]['id'] in s['inputs']]
        self.assertTrue(later)
        self.assertEqual('passed', later[-1]['outcome'])

    def test_repair_decisions_link_to_rejected_acceptance(self):
        from test_review_history import ContinuityEngineTest
        fixture = ContinuityEngineTest('test_three_repairs_then_limit_and_raise_without_repeating_completed_review')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with trace.operation(self.root, True, 'session'):
            fixture.test_three_repairs_then_limit_and_raise_without_repeating_completed_review()
        data = self.data()
        by_id = {s['id']: s for s in data['stages']}
        repairs = [s for s in data['stages'] if s['role'] == 'control/repair_allocated']
        self.assertEqual(4, len(repairs))
        for repair in repairs:
            self.assertTrue(any(by_id[source]['role'] == 'acceptance' and by_id[source]['outcome'] == 'changes_requested'
                                for source in repair['inputs']))

    def test_correction_creates_distinct_attempt_and_causal_link(self):
        with trace.operation(self.root, True, 'session'):
            with trace.stage('planning', key='attempt') as handle:
                first = handle[1]['id']
                trace.restart(handle, 'format_correction', 'Malformed response')
                final = trace.complete(handle, {'decision': 'plan'})
        nodes = {s['id']: s for s in self.data()['stages']}
        self.assertNotEqual(first, final)
        self.assertEqual('failed', nodes[first]['status'])
        self.assertEqual('plan', nodes[final]['outcome'])
        self.assertEqual(first, nodes[final]['retry_of'])
        self.assertTrue(any(nodes[source]['role'] == 'control/format_correction' for source in nodes[final]['inputs']))
