"""Controller verification fills evidence gaps without another implementation turn."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from engine_fixture import ScriptedProviders
import test_issue_engine
from pr_review import owner_git, issue_verification, test_evidence
from pr_review.common import ApiError


class VerificationRecoveryTest(ScriptedProviders, unittest.TestCase):
    workspace_name = test_issue_engine.IssueEngineTest.workspace_name
    setUp = test_issue_engine.IssueEngineTest.setUp
    last = test_issue_engine.IssueEngineTest.last
    command_text = 'PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_verification_case'

    def fix(self, session, state, batch, specification, feedback):
        report = test_issue_engine.IssueEngineTest.fix(self, session, state, batch, specification, feedback)
        (Path(state['worktree']) / 'test_verification_case.py').write_text(
            getattr(self, 'test_source', 'import unittest\nclass Check(unittest.TestCase):\n'
                    '    def test_candidate(self):\n        self.assertTrue(True)\n'))
        batch['checkpoint'] = owner_git.checkpoint(session, state, 'verification fixture')
        self.verdict('incomplete')
        value = json.loads(self.result_file.read_text())
        value['verification_blockers'] = [{'command': self.command_text, 'category': 'evidence',
            'reason': 'Command-time source identity unknown', 'manual_command': self.command_text}]
        self.result_file.write_text(json.dumps(value))
        return report

    def test_missing_source_identity_collects_candidate_evidence_and_resumes_synthesis(self):
        from pr_review import issue_verification
        collect = issue_verification.collect
        def recover(session, state, batch, blockers):
            result = collect(session, state, batch, blockers)
            if result:
                records = issue_verification.index(batch)
                self.assertEqual(1, len(records))
                record = records[0]
                self.assertEqual(0, record['exit_code'])
                self.assertEqual(batch['checkpoint']['tree'], record['candidate']['tree'])
                self.assertNotEqual(state['worktree'], record['cwd'])
                self.assertEqual('verified', record['source_check'])
                self.assertIn('Ran 1 test', Path(record['stderr']['path']).read_text())
                self.verdict('passed')
            return result
        with patch('pr_review.issue_harness.invoke', side_effect=self.fix), \
                patch('pr_review.issue_verification.approved_commands', return_value={self.command_text}), \
                patch('pr_review.issue_verification.collect', side_effect=recover):
            result = self.f.start(issue_harness=True)
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(1, len(self.turns))
        self.assertEqual(2, sum(line.startswith('synthesis cwd=') for line in self.marks.read_text().splitlines()))
        self.assertIn('controller_verification', self.stdin_copy.read_text())
        self.assertEqual([], self.last()['review_history']['cycles'])

    def test_independent_reports_are_reused_after_controller_verification(self):
        state = self.session.load()
        state['settings']['final_review']['reviewer_slots'] = self.SLOTS
        self.session.save(state)
        self.test_missing_source_identity_collects_candidate_evidence_and_resumes_synthesis()
        commands = self.commands()
        for slot in ('reviewer-1', 'reviewer-2'):
            self.assertEqual(1, sum(c['stage'] == slot for c in commands), commands)

    def pending(self):
        with patch('pr_review.issue_harness.invoke', side_effect=self.fix), \
                patch('pr_review.issue_verification.collect', return_value=False), self.assertRaises(ApiError) as caught:
            self.f.start(issue_harness=True)
        self.assertEqual('review_incomplete', caught.exception.code)
        state = self.session.load()
        return state, state['batches'][-1]

    def collect(self, state, batch):
        with patch('pr_review.issue_verification.approved_commands', return_value={self.command_text}):
            return issue_verification.collect(self.session, state, batch,
                [{'command': self.command_text, 'category': 'evidence'}])

    def test_failed_command_does_not_repeat_or_allow_acceptance(self):
        self.test_source = 'import unittest\nclass Check(unittest.TestCase):\n    def test_bad(self): self.fail("broken")\n'
        state, batch = self.pending()
        self.assertTrue(self.collect(state, batch))
        record = issue_verification.index(batch)[0]
        self.assertEqual(('failed', 1, 'verified'), (record['status'], record['exit_code'], record['source_check']))
        self.assertFalse(self.collect(state, batch))
        extra = self.command_text + ' -v'
        with patch('pr_review.issue_verification.approved_commands', return_value={self.command_text, extra}):
            self.assertFalse(issue_verification.collect(self.session, state, batch,
                [{'command': extra, 'category': 'evidence'}]))
        self.assertEqual(1, len(batch['verification_runs']))
        self.verdict('passed')
        with self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'])
        self.assertEqual('invalid_result', caught.exception.code)
        self.assertNotIn('final_tree', self.last())

    def test_output_limit_stops_and_retains_nonpassing_receipt(self):
        self.test_source = 'print("x" * 200000)\n'
        state, batch = self.pending()
        with patch.object(issue_verification, 'MAX_LOG_BYTES', 100000):
            self.assertTrue(self.collect(state, batch))
            record = issue_verification.index(batch)[0]
        self.assertEqual('unavailable', record['status'])
        self.assertIn('output byte limit', record['error'])
        self.assertIsNone(record['exit_code'])
        self.assertLessEqual(record['stdout']['bytes'], 100000)
        self.assertFalse(self.collect(state, batch))

    def test_timeout_is_durable_and_not_retried(self):
        self.test_source = 'import time\ntime.sleep(5)\n'
        state, batch = self.pending()
        with patch.object(issue_verification, 'MAX_SECONDS', 0.3):
            self.assertTrue(self.collect(state, batch))
        record = issue_verification.index(batch)[0]
        self.assertEqual('timeout', record['status'])
        self.assertEqual(124, record['exit_code'])
        self.assertFalse(self.collect(state, batch))

    def test_modified_snapshot_is_rejected_without_modifying_live_checkout(self):
        self.test_source = 'from pathlib import Path\nPath("source.txt").write_text("changed")\n'
        state, batch = self.pending()
        original = (Path(state['worktree']) / 'source.txt').read_bytes()
        self.assertTrue(self.collect(state, batch))
        record = issue_verification.index(batch)[0]
        self.assertEqual('failed', record['source_check'])
        self.assertNotEqual('completed', record['status'])
        self.assertEqual(original, (Path(state['worktree']) / 'source.txt').read_bytes())
        self.assertNotIn('integrity_failure', batch)

    def test_log_tampering_invalidates_receipt_and_acceptance_proof(self):
        state, batch = self.pending()
        self.collect(state, batch)
        batch.update(final_tree=batch['checkpoint']['tree'], harness_evidence_proof={
            'tree': batch['checkpoint']['tree'], 'bundles': [],
            'controller_verification': issue_verification.references(batch)})
        self.assertTrue(test_evidence.proof_current(batch))
        record = issue_verification.index(batch)[0]
        Path(record['stderr']['path']).write_text('forged success')
        with self.assertRaises(ApiError) as caught:
            issue_verification.index(batch)
        self.assertEqual('test_evidence_integrity', caught.exception.code)
        self.assertFalse(test_evidence.proof_current(batch))

    def test_running_intent_recovers_unknown_outcome_without_reexecution(self):
        state, batch = self.pending()
        self.collect(state, batch)
        record = batch['verification_runs'][0]
        record['status'] = 'running'
        record.pop('reference')
        self.session.save(state)
        with patch('pr_review.issue_verification.processes.run', side_effect=AssertionError('must not execute')):
            self.assertTrue(self.collect(state, batch))
        result = issue_verification.index(batch)[0]
        self.assertEqual('interrupted', result['status'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual('unknown', result['source_check'])
        self.assertFalse(self.collect(state, batch))

    def test_explicit_synthesis_rerun_allows_one_new_attempt(self):
        state, batch = self.pending()
        self.collect(state, batch)
        self.assertFalse(self.collect(state, batch))
        batch['review_reruns'] = {'synthesis': 'explicit-retry'}
        self.session.save(state)
        self.assertTrue(self.collect(state, batch))
        self.assertFalse(self.collect(state, batch))
        self.assertEqual([None, 'explicit-retry'], [r['epoch'] for r in issue_verification.index(batch)])

    def test_changed_candidate_requires_new_verification(self):
        state, batch = self.pending()
        self.collect(state, batch)
        old_tree = batch['checkpoint']['tree']
        (Path(state['worktree']) / 'source.txt').write_text('new candidate\n')
        batch['checkpoint'] = owner_git.checkpoint(self.session, state, 'next candidate')
        self.session.save(state)
        self.assertTrue(self.collect(state, batch))
        self.assertEqual([old_tree, batch['checkpoint']['tree']],
                         [r['candidate']['tree'] for r in issue_verification.index(batch)])

    def test_command_count_bound_is_preserved_on_resume(self):
        state, batch = self.pending()
        commands = [self.command_text, self.command_text + ' -v']
        with patch.object(issue_verification, 'MAX_COMMANDS', 1), \
                patch('pr_review.issue_verification.approved_commands', return_value=set(commands)):
            blockers = [{'command': c, 'category': 'evidence'} for c in commands]
            self.assertTrue(issue_verification.collect(self.session, state, batch, blockers))
            self.assertFalse(issue_verification.collect(self.session, state, batch, blockers))
        self.assertEqual(1, len(issue_verification.index(batch)))

    def test_unapproved_commands_and_environment_blockers_are_not_executed(self):
        state, batch = self.pending()
        with patch('pr_review.issue_verification.processes.run', side_effect=AssertionError('must not execute')):
            self.assertFalse(issue_verification.collect(self.session, state, batch,
                [{'command': self.command_text, 'category': 'evidence'}]))
            with patch('pr_review.issue_verification.approved_commands', return_value={self.command_text}):
                self.assertFalse(issue_verification.collect(self.session, state, batch,
                    [{'command': self.command_text, 'category': 'environment'}]))
        self.assertEqual([], batch['verification_runs'])


class CommandSelectionTest(unittest.TestCase):
    def test_only_literal_supported_approved_checks_are_selected(self):
        document = {'text': '## Verification\nRun `make check-backend` and `PYTHONPATH=lib:tests python3 -m unittest test_api`.\n'
                           '```sh\npytest tests\nmake smoke\nmake check; echo extra\n```\n'
                           'Do not run `make test-integration`.\n'
                           'Optional: `make lint`.\n'
                           '## Implementation\nExample: `make verify`.\n'
                           'Plain prose: make test\n'}
        with patch('pr_review.issue_verification.specification.handoff', return_value=document):
            self.assertEqual({'make check-backend', 'PYTHONPATH=lib:tests python3 -m unittest test_api', 'pytest tests'},
                             issue_verification.approved_commands(None, None))
        for command in ('make smoke', 'make check; echo bad', 'make check | tee log', 'make check && true',
                        'HOME=/tmp make check', 'PATH=/tmp make check', 'PYTHONPATH=/tmp python3 -m unittest',
                        'python3 -c "print(1)"', 'make -f /tmp/Makefile check', 'make check$(touch bad)',
                        '/tmp/pytest', 'npm install', 'pytest `pwd`'):
            with self.subTest(command=command):
                self.assertIsNone(issue_verification.parse_command(command))
