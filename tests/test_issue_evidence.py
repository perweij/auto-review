"""Actual issue invoke/execute and shared-engine recovery with mocked providers."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_issue_steps
from engine_fixture import ScriptedProviders
from test_test_evidence import attempt, declaration
from pr_review import acceptance, issue_harness, owner_contracts, owner_git, test_evidence as evidence
from pr_review.common import ApiError, read_json
from pr_review.storage import write_json

REAL_INVOKE = issue_harness.invoke
REAL_CAPTURE = evidence.capture


class IssueEvidenceTest(ScriptedProviders, unittest.TestCase):
    workspace_name = 'issue-acceptance'

    def setUp(self):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.base = self.f.fixture
        self.keys = [self.f.key]
        self.session = self.f.owner
        self.install_providers()
        state = self.session.load()
        state['settings']['fix']['provider'] = 'codex'
        state['settings']['final_review'] = {'reviewer_slots': [{'id': 'reviewer-1', **self.SYNTHESIS}],
                                             'synthesis': dict(self.SYNTHESIS), 'synthesis_tests': 'focused'}
        self.session.save(state)
        self.implementation_calls = 0
        self.verdict(summary='Implementation execution records show make check-backend passed')
        patcher = patch('pr_review.issue_harness.invoke', new=REAL_INVOKE)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch('pr_review.issue_harness.run', side_effect=self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def transport(self, argv, **kwargs):
        self.implementation_calls += 1
        cwd = Path(kwargs['cwd'])
        os.environ['OWNER_WORKTREE'] = str(cwd)
        (cwd / 'implementation.txt').write_text(f'implementation {self.implementation_calls}\n')
        scratch = Path(kwargs['env']['TMPDIR'])
        (scratch / 'suite.log').write_text('Ran 415 tests\nOK\n')
        tree = owner_git.checkpoint(self.session, self.session.load(), 'fixture execution')['tree']
        report = {'status': 'ready', 'summary': 'Candidate complete', 'checks': ['aggregate passed'],
                  'findings': [], 'questions': [], 'test_evidence': [declaration(cwd=str(cwd), tested_tree=tree, tool_id='cmd-1')]}
        events = [
            {'type': 'thread.started', 'thread_id': 'fixed-thread'},
            {'type': 'item.started', 'item': {'type': 'command_execution', 'id': 'cmd-1', 'command': 'make check-backend',
                                              'status': 'in_progress', 'exit_code': None, 'aggregated_output': ''}},
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'cmd-1', 'command': 'make check-backend',
                                                'status': 'completed', 'exit_code': 0, 'aggregated_output': 'Ran 415 tests\nOK\n'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'UNRELATED_IMPLEMENTATION_REASONING'}},
            {'type': 'turn.completed'}]
        Path(kwargs['stdout_path']).write_text(''.join(json.dumps(e) + '\n' for e in events))
        for event in events:
            kwargs['on_line']('stdout', json.dumps(event))
        write_json(Path(kwargs['stdout_path']).with_name('result.json'), report)
        return SimpleNamespace(returncode=0, events=events)

    def start(self, **fields):
        return self.f.start(issue_harness=True, **fields)

    def last(self):
        self.state = self.session.load()
        self.batch = self.state['batches'][-1]
        return self.batch

    def review_again(self):
        self.last()
        return acceptance.review_issue(self.session, self.state, self.batch, {'revision': 1})

    def test_recorded_aggregate_reaches_focused_synthesis_and_exact_candidate_commit_guard(self):
        view = self.start(auto_commit=True)
        self.last()
        batch = view['batches'][-1]
        self.assertTrue(owner_contracts.current(batch))
        self.assertTrue(batch['commit_progress']['complete'])
        self.assertEqual(1, self.implementation_calls)
        self.assertEqual({'synthesis': 1}, self.counts())
        self.assertFalse(any('make check-backend' in entry['argv'] for entry in self.commands()))
        prompt = self.stdin_copy.read_text()
        self.assertIn('sufficiently supported implementation evidence', prompt)
        self.assertIn('category=evidence', prompt)
        self.assertNotIn('claims to assess, not\nevidence', prompt)
        self.assertNotIn('observed error and required environment', prompt)
        self.assertNotIn('UNRELATED_IMPLEMENTATION_REASONING', prompt)
        index = read_json(self.workspace() / 'context/implementation_test_evidence.json')
        bundle = index['bundles'][0]
        self.assertEqual(batch['harness_candidate_tree'], bundle['controller']['after']['tree'])
        self.assertEqual('unknown', bundle['commands'][0]['source_identity'])
        self.assertEqual(0, bundle['commands'][0]['exit_code'])
        self.assertEqual('unchanged', bundle['controller']['protected_state'])
        self.assertNotIn('Ran 415 tests', prompt)
        self.assertIn('category', read_json(self.workspace() / 'prompts/synthesis.schema.json')
                      ['properties']['verification_blockers']['items']['required'])

    def test_result_saved_before_capture_resumes_without_implementation(self):
        with patch.object(evidence, 'capture', side_effect=OSError('capture crash')), self.assertRaises(OSError):
            self.start()
        batch = self.last()
        source = batch['attempts'][-1]
        self.assertEqual('capture_failed', source['status'])
        self.assertEqual('completed', source['provider_completion'])
        self.assertIn('test_evidence', source['result'])
        self.assertEqual('ready', self.start(batch=batch['id'])['batches'][-1]['status'])
        self.assertEqual(1, self.implementation_calls)
        self.assertEqual(1, self.counts()['synthesis'])

    def test_bundle_finalized_before_reference_save_recovers_frozen_bytes(self):
        frozen = []
        def crash(record):
            reference = REAL_CAPTURE(record)
            frozen.append(reference)
            raise OSError('crash after freezing bundle')
        with patch.object(evidence, 'capture', side_effect=crash), self.assertRaises(OSError):
            self.start()
        batch = self.last()
        self.assertNotIn('test_evidence_bundle', batch['attempts'][-1])
        self.start(batch=batch['id'])
        batch = self.last()
        self.assertEqual(frozen[0], batch['attempts'][0]['test_evidence_bundle'])
        self.assertEqual(1, self.implementation_calls)

    def test_completed_capture_before_phase_transition_is_not_consumed_on_crash(self):
        with patch.object(issue_harness, 'consume_result', side_effect=OSError('crash before transition save')), self.assertRaises(OSError):
            self.start()
        batch = self.last()
        self.assertFalse(batch['attempts'][0].get('implementation_consumed'))
        self.start(batch=batch['id'])
        self.assertEqual(1, self.implementation_calls)
        self.assertEqual('ready', self.last()['status'])

    def test_pending_old_candidate_does_not_replace_explicitly_adopted_edits(self):
        with patch.object(evidence, 'capture', side_effect=OSError('capture crash')), self.assertRaises(OSError):
            self.start()
        batch = self.last()
        old_tree = batch['checkpoint']['tree']
        (Path(self.state['worktree']) / 'human-edit.txt').write_text('adopted source\n')
        with self.assertRaises(ApiError):
            self.start(batch=batch['id'])
        self.start(batch=batch['id'], adopt=True)
        batch = self.last()
        self.assertEqual(2, self.implementation_calls)
        self.assertEqual('stale_candidate', batch['attempts'][0]['candidate_recovery'])
        bundles = read_json(self.workspace() / 'context/implementation_test_evidence.json')['bundles']
        self.assertEqual(2, len(bundles))
        self.assertEqual(old_tree, bundles[0]['controller']['after']['tree'])
        self.assertNotEqual(old_tree, bundles[1]['controller']['after']['tree'])
        self.assertEqual(old_tree, bundles[0]['declarations'][0]['tested_tree'])

    def test_cache_reuse_after_synthesis_does_not_invoke_any_model(self):
        self.start()
        result, _ = self.review_again()
        self.assertTrue(result.reused)
        self.assertEqual(1, self.counts()['synthesis'])
        self.assertEqual(1, self.implementation_calls)

    def test_mutated_evidence_rejects_cached_acceptance_and_later_publication_proof(self):
        self.start()
        batch = self.last()
        self.assertTrue(owner_contracts.current(batch))
        reference = batch['attempts'][0]['test_evidence_bundle']
        (Path(reference['attempt_path']) / 'test-evidence/logs/0-0.log').write_text('changed')
        self.assertFalse(owner_contracts.current(batch))
        with self.assertRaises(ApiError) as caught:
            self.review_again()
        self.assertEqual('test_evidence_integrity', caught.exception.code)
        self.assertEqual(1, self.counts()['synthesis'])

    def test_new_evidence_invalidates_synthesis_but_preserves_independent_reports_and_plan(self):
        # Two real independent slots with mocked CLIs exercise cache boundaries.
        state = self.session.load()
        state['settings']['final_review'] = {'reviewer_slots': copy.deepcopy(self.SLOTS),
                                             'synthesis': dict(self.SYNTHESIS), 'synthesis_tests': 'focused'}
        self.session.save(state)
        self.start()
        batch = self.last()
        before_phases = copy.deepcopy(self.manifest()['phases'])
        before_plan = copy.deepcopy(self.state['feedback'])
        candidate = copy.deepcopy(batch['checkpoint'])
        added = attempt(self.session.path / 'attempts/added-evidence')
        added.update(status='complete', capture_status='complete', implementation_consumed=True)
        added['test_evidence_bundle'] = REAL_CAPTURE(added)
        batch['attempts'].append(added)
        self.session.save(self.state)
        result, _ = self.review_again()
        self.assertFalse(result.reused)
        self.assertEqual({'reviewer-1': 1, 'reviewer-2': 1, 'synthesis': 2}, self.counts())
        for slot in ('reviewer-1', 'reviewer-2'):
            self.assertEqual(before_phases[slot]['fingerprint'], self.manifest()['phases'][slot]['fingerprint'])
        self.assertEqual(candidate, self.last()['checkpoint'])
        self.assertEqual(before_plan, self.state['feedback'])
        self.assertEqual(1, self.implementation_calls)

    def test_incomplete_blocker_categories_flow_through_actual_execute(self):
        cases = [('environment', 'verify_environment'), ('evidence', 'verify_evidence'),
                 ('policy', 'review_policy'), (None, 'resolve_verification_blockers'),
                 (['evidence', 'policy'], 'resolve_verification_blockers')]
        identifier = None
        for categories, action in cases:
            self.verdict('incomplete', summary='Concrete verification limitation')
            value = read_json(self.result_file)
            categories = categories if isinstance(categories, list) else [categories]
            value['verification_blockers'] = [
                {'command': 'make check-backend', 'reason': 'See command/log artifact for the concrete gap',
                 'manual_command': 'make check-backend', **({'category': c} if c else {})} for c in categories]
            write_json(self.result_file, value)
            with self.assertRaises(ApiError) as caught:
                self.start(**({'batch': identifier} if identifier else {}))
            self.assertEqual('review_incomplete', caught.exception.code)
            self.assertEqual(action, caught.exception.details['next_action'])
            batch = self.last()
            identifier = batch['id']
            self.assertNotIn('final_tree', batch)
            self.assertFalse(owner_contracts.current(batch))
        self.assertEqual(1, self.implementation_calls)

    def test_claude_unknown_numeric_status_remains_evidence_blocker(self):
        state = self.session.load()
        state['settings']['fix']['provider'] = 'claude'
        self.session.save(state)
        self.verdict('incomplete', summary='Implementation command numeric status is unknown')
        value = read_json(self.result_file)
        value['verification_blockers'] = [{'category': 'evidence', 'command': 'make check-backend',
            'reason': 'Claude Bash result lacks a numeric exit status; inspect manifest',
            'manual_command': 'make check-backend'}]
        write_json(self.result_file, value)
        def claude(argv, **kwargs):
            self.implementation_calls += 1
            cwd = Path(kwargs['cwd'])
            (cwd / 'implementation.txt').write_text('implementation\n')
            scratch = Path(kwargs['env']['TMPDIR'])
            (scratch / 'suite.log').write_text('Ran 415 tests\nOK\n')
            tree = owner_git.checkpoint(self.session, self.session.load(), 'fixture execution')['tree']
            report = {'status': 'ready', 'summary': 'Candidate complete', 'checks': ['aggregate passed'],
                      'findings': [], 'questions': [],
                      'test_evidence': [declaration(cwd=str(cwd), tested_tree=tree, tool_id='bash-1')]}
            events = [
                {'type': 'system', 'subtype': 'init', 'session_id': argv[argv.index('--session-id') + 1]},
                {'type': 'assistant', 'uuid': 'a-1', 'message': {'content': [
                    {'type': 'tool_use', 'id': 'bash-1', 'name': 'Bash', 'input': {'command': 'make check-backend'}}]}},
                {'type': 'user', 'uuid': 'u-1', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'bash-1', 'content': 'Ran 415 tests\nOK', 'is_error': False}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': report}]
            Path(kwargs['stdout_path']).write_text(''.join(json.dumps(e) + '\n' for e in events))
            for event in events:
                kwargs['on_line']('stdout', json.dumps(event))
            return SimpleNamespace(returncode=0, events=events)
        with patch('pr_review.issue_harness.run', side_effect=claude), self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('verify_evidence', caught.exception.details['next_action'])
        self.last()
        bundle = read_json(self.workspace() / 'context/implementation_test_evidence.json')['bundles'][0]
        self.assertEqual('claude', bundle['controller']['provider'])
        self.assertIsNone(bundle['commands'][0]['exit_code'])
        self.assertEqual('completed', bundle['commands'][0]['completion'])
        self.assertEqual(1, self.implementation_calls)
        self.assertNotIn('final_tree', self.batch)

    def test_integrity_checked_after_synthesis_execution(self):
        real_structured = acceptance.AcceptanceAdapter.structured
        def mutate(adapter, run, stage):
            result = real_structured(adapter, run, stage)
            reference = adapter.evidence_references[0]
            (Path(reference['attempt_path']) / 'test-evidence/logs/0-0.log').write_text('tampered after process')
            return result
        # Mutation after model returns but before acceptance must also be caught.
        with patch.object(acceptance.AcceptanceAdapter, 'structured', new=mutate), self.assertRaises(ApiError):
            self.start()
        self.assertFalse(owner_contracts.current(self.last()))
        self.assertNotIn('final_tree', self.batch)
