"""Recorded provider fixtures, immutable artifacts and issue-only handoff contracts."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pr_review import acceptance, issue_harness, test_evidence as evidence
from pr_review.common import ApiError
from pr_review.storage import write_json

FIXTURES = Path(__file__).parent / 'fixtures/test-evidence'


def declaration(command='make check-backend', **fields):
    return {'command': command, 'cwd': '/candidate', 'log_paths': ['suite.log'], 'tested_tree': 'claimed-tree',
            'summary': 'Implementation reports suite passed', 'tool_id': None, 'event_id': None, **fields}


def attempt(root, declared=None, provider='codex'):
    root.mkdir(exist_ok=True)
    (root / 'scratch').mkdir(exist_ok=True)
    (root / 'scratch/suite.log').write_text('Ran 415 tests\nOK\n')
    (root / 'events.jsonl').write_bytes((FIXTURES / f'{provider}.jsonl').read_bytes())
    value = {'status': 'ready', 'summary': 'Candidate complete', 'checks': ['Implementation reports success'],
             'findings': [], 'questions': [], 'test_evidence': declared if declared is not None else [declaration()]}
    write_json(root / 'result.json', value)
    return {'kind': 'issue_harness', 'path': str(root), 'test_evidence_version': 1,
            'choice': {'provider': provider, 'model': 'fixture', 'effort': 'high'},
            'before': {'tree': 'same-tree'}, 'after': {'tree': 'same-tree'},
            'protected_state': 'unchanged', 'provider_completion': 'completed', 'result': value}


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'attempt'
        self.record = attempt(self.root)

    def capture(self):
        reference = evidence.capture(self.record)
        return reference, evidence.verify(reference)

    def test_codex_exact_outcomes_compound_ambiguity_and_selected_data(self):
        rows = [declaration(), declaration('make check-emacs'), declaration('python3 -m unittest'),
                declaration('echo one &&\necho two | tee test.log'), declaration('make repeated'),
                declaration('make repeated', tool_id='cmd-6'), declaration('make long'), declaration('make warning'), declaration('echo two')]
        self.record = attempt(self.root, rows)
        reference, manifest = self.capture()
        records = manifest['commands']
        self.assertEqual([0, 2, None, 0, 1, 0, 0], [r['exit_code'] for r in records])
        self.assertEqual('incomplete', records[2]['completion'])
        self.assertEqual('echo one &&\necho two | tee test.log', records[3]['command'])
        self.assertEqual('cmd-1', records[0]['tool_id'])
        self.assertEqual(['line:3', 'line:4'], records[0]['event_ids'])
        self.assertTrue(records[-1]['output']['truncated'])
        self.assertEqual('reported', records[-1]['output']['provider_truncation'])
        self.assertTrue(records[-2]['output']['truncated'])
        self.assertEqual(['ambiguous recorded command', 'no exact recorded command'],
                         [g['reason'] for g in manifest['gaps']])
        frozen = json.dumps(manifest) + ''.join(p.read_text() for p in (self.root / 'test-evidence/commands').iterdir())
        self.assertNotIn('UNRELATED', frozen)
        self.assertIsNone(records[0]['cwd'])
        self.assertEqual('unknown', records[0]['source_identity'])
        self.assertNotIn('verified', frozen)
        self.assertIn('equal before/after checkpoints do not establish', manifest['limitations'])
        self.assertEqual(reference, evidence.capture(self.record))

    def test_claude_non_error_without_numeric_status_stays_unknown(self):
        rows = [declaration(), declaration('make check-emacs'), declaration('echo one &&\necho two | tee test.log')]
        self.record = attempt(self.root, rows, provider='claude')
        _, manifest = self.capture()
        self.assertEqual([None, None, 0], [r['exit_code'] for r in manifest['commands']])
        self.assertEqual(['completed', 'failed', 'completed'], [r['completion'] for r in manifest['commands']])
        self.assertEqual(['a-1', 'u-1'], manifest['commands'][0]['event_ids'])
        self.assertEqual('bash-1', manifest['commands'][0]['tool_id'])
        self.assertNotIn('UNRELATED', json.dumps(manifest))

    def test_explicit_event_id_disambiguates_without_relaxing_exact_command(self):
        records, _ = evidence.normalize(self.root, 'codex')
        row, gap = evidence.match(declaration('make repeated', event_id='line:9'), records)
        self.assertIsNone(gap)
        self.assertEqual('cmd-6', row['tool_id'])
        row, gap = evidence.match(declaration('make backend', tool_id='cmd-1'), records)
        self.assertIsNone(row)
        self.assertEqual('no exact recorded command', gap)

    def test_log_hazards_are_gaps_without_following_links_or_blocking_fifo(self):
        outside = self.root.parent / 'outside.log'
        outside.write_text('must not copy')
        (self.root / 'scratch/link.log').symlink_to(outside)
        (self.root / 'scratch/parent').symlink_to(self.root.parent, target_is_directory=True)
        (self.root / 'scratch/directory').mkdir()
        os.mkfifo(self.root / 'scratch/pipe')
        paths = ['../outside.log', str(outside), 'link.log', 'parent/outside.log', 'directory', 'pipe', 'missing']
        self.record = attempt(self.root, [declaration(log_paths=paths)])
        _, manifest = self.capture()
        self.assertEqual([], manifest['logs'])
        self.assertEqual(7, len(manifest['gaps']))
        self.assertNotIn('must not copy', json.dumps(manifest))

    def test_bounded_log_and_output_ingestion_is_explicit(self):
        with patch.object(evidence, 'MAX_LOG', 5), patch.object(evidence, 'MAX_OUTPUT', 3):
            _, manifest = self.capture()
            self.assertEqual([], manifest['logs'])
            self.assertIn('ingestion limit', manifest['gaps'][0]['reason'])
            output = manifest['commands'][0]['output']
            self.assertTrue(output['capture_truncated'])
            self.assertTrue(output['truncated'])
            self.assertEqual(3, output['bytes'])
        # A different limit also changes normalized output: fail rather than
        # silently reinterpreting an earlier frozen capture.
        with self.assertRaises(ApiError):
            evidence.verify(evidence.capture(self.record))

    def test_missing_or_oversized_stream_cannot_manufacture_execution(self):
        for mode in ('missing', 'oversized'):
            with self.subTest(mode=mode):
                root = self.root.parent / mode
                row = attempt(root)
                if mode == 'missing':
                    (root / 'events.jsonl').unlink()
                    reference = evidence.capture(row)
                else:
                    with patch.object(evidence, 'MAX_STREAM', 12000):
                        (root / 'events.jsonl').write_text('X' * 12001)
                        reference = evidence.capture(row)
                manifest = json.loads((root / 'test-evidence/manifest.json').read_text())
                self.assertEqual([], manifest['commands'])
                self.assertTrue(manifest['stream_gaps'])
                self.assertEqual('no exact recorded command', manifest['gaps'][0]['reason'])
                if mode == 'missing':
                    evidence.verify(reference)

    def test_original_copied_manifest_and_result_tampering_fail_closed(self):
        for target in ('scratch/suite.log', 'test-evidence/logs/0-0.log', 'test-evidence/commands/0.txt',
                       'test-evidence/manifest.json', 'result.json', 'events.jsonl'):
            with self.subTest(target=target):
                root = self.root.parent / target.replace('/', '-')
                row = attempt(root)
                reference = evidence.capture(row)
                path = root / target
                path.write_bytes(path.read_bytes().replace(b'OK', b'NO') if target == 'events.jsonl' else b'changed')
                with self.assertRaises(ApiError) as caught:
                    evidence.verify(reference)
                self.assertEqual('test_evidence_integrity', caught.exception.code)
                row['test_evidence_bundle'] = reference
                with self.assertRaises(ApiError):
                    evidence.capture(row)

    def test_missing_artifact_and_source_log_symlink_after_freeze_fail(self):
        reference, _ = self.capture()
        (self.root / 'scratch/suite.log').unlink()
        (self.root / 'scratch/suite.log').symlink_to(self.root / 'test-evidence/logs/0-0.log')
        with self.assertRaises(ApiError):
            evidence.verify(reference)
        (self.root / 'test-evidence/commands/0.txt').unlink()
        with self.assertRaises(ApiError):
            evidence.verify(reference)

    def test_sealed_staging_and_unreferenced_final_recover_same_bytes(self):
        real_rename = os.rename
        with patch.object(evidence.os, 'rename', side_effect=OSError('crash at rename')), self.assertRaises(OSError):
            evidence.capture(self.record)
        self.assertTrue((self.root / 'test-evidence.pending/manifest.json').is_file())
        self.assertFalse((self.root / 'test-evidence').exists())
        with patch.object(evidence.os, 'rename', wraps=real_rename):
            reference = evidence.capture(self.record)
        frozen = (self.root / 'test-evidence/manifest.json').read_bytes()
        self.assertEqual(reference, evidence.capture(self.record))  # crash before state reference save
        self.assertEqual(frozen, (self.root / 'test-evidence/manifest.json').read_bytes())

    def test_unsealed_partial_never_reused_and_altered_sealed_bytes_never_regenerated(self):
        pending = self.root / 'test-evidence.pending'
        pending.mkdir()
        (pending / 'manifest.json').write_text('partial')
        reference, _ = self.capture()
        (self.root / 'test-evidence/manifest.json').write_text('altered')
        with self.assertRaises(ApiError):
            evidence.capture(self.record)
        self.assertTrue(reference['sha256'])

    def test_truncated_log_is_explicit_and_malformed_claude_fields_are_gaps(self):
        (self.root / 'scratch/suite.log').write_text('[output truncated]\nOK')
        _, manifest = self.capture()
        self.assertTrue(manifest['logs'][0]['truncated'])
        self.assertEqual('reported', manifest['logs'][0]['provider_truncation'])
        malformed = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': {'bad': 'identity'}, 'content': 'must not copy'}]}}
        (self.root / 'events.jsonl').write_text(json.dumps(malformed) + '\n')
        records, gaps = evidence.normalize(self.root, 'claude')
        self.assertEqual([], records)
        self.assertIn('invalid tool identity', gaps[0])
        self.assertNotIn('must not copy', json.dumps(gaps))

    def test_legacy_and_new_schema_are_separate(self):
        legacy = {k: v for k, v in self.record['result'].items() if k != 'test_evidence'}
        self.assertEqual(legacy, issue_harness.validate(legacy))
        self.assertNotIn('test_evidence', issue_harness.SCHEMA['properties'])
        self.assertIn('test_evidence', issue_harness.EVIDENCE_SCHEMA['required'])
        with self.assertRaises(ApiError):
            issue_harness.validate_evidence(legacy)
        self.assertEqual([], issue_harness.validate_evidence({**legacy, 'test_evidence': []})['test_evidence'])

    def test_blocker_categories_and_legacy_actions(self):
        self.assertEqual('retry', issue_harness.verification_action([]))
        for category, action in [('environment', 'verify_environment'), ('evidence', 'verify_evidence'), ('policy', 'review_policy')]:
            self.assertEqual(action, issue_harness.verification_action([{'category': category}]))
        for blockers in ([{}], [{'category': 'evidence'}, {'category': 'policy'}], [{'category': 'environment'}, {}]):
            self.assertEqual('resolve_verification_blockers', issue_harness.verification_action(blockers))
        value = {'assessment': 'incomplete', 'summary': 'Evidence missing', 'checks': [], 'findings': [],
                 'outcomes': [], 'case_results': [], 'questions': [], 'verification_blockers': []}
        def validate(value, issue=True):
            return acceptance.validate_acceptance(value, keys=[], exact_outcomes=False, cases=[], tree='tree',
                                                  sources={'new'}, issue_categories=issue)
        for category in ('environment', 'evidence', 'policy', None):
            blocker = {'command': 'make check', 'reason': 'concrete gap', 'manual_command': 'make check'}
            if category:
                blocker['category'] = category
            value['verification_blockers'] = [blocker]
            validate(value)
            with self.assertRaises(ApiError):
                validate({**value, 'assessment': 'passed'})
        value['verification_blockers'][0]['category'] = 'invented'
        with self.assertRaises(ApiError):
            validate(value)
        value['verification_blockers'][0]['category'] = 'evidence'
        with self.assertRaises(ApiError):
            validate(value, issue=False)  # Owner contract unchanged
