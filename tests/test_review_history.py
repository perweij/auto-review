"""Issue-only continuity, durable transitions and focused provider orchestration."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine_fixture import ScriptedProviders, finding
import test_issue_engine
from pr_review import acceptance, review_history as history, owner_git
from pr_review.common import ApiError


def verdict(findings=(), *, lineage=(), dispositions=(), assessment='changes_requested'):
    return {'assessment': assessment, 'summary': 'Review', 'checks': [], 'findings': list(findings),
            'questions': [], 'outcomes': [], 'case_results': [], 'finding_lineage': list(lineage),
            'previous_dispositions': list(dispositions)}


def disposition(key='R1', status='open', evidence='new evidence', reason='Inspected current source'):
    return {'finding_id': key, 'status': status, 'evidence': evidence, 'reason': reason}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='continuity-ledger-')
        self.addCleanup(self.tmp.cleanup)
        self.artifact = Path(self.tmp.name) / 'synthesis.json'
        self.batch = {'checkpoint': {'tree': 'tree0'}}
        history.initialize(self.batch, {})

    def ingest(self, value, tree='tree0'):
        key, frozen = history.snapshot(self.batch, tree)
        self.artifact.write_text(json.dumps(value))
        return history.ingest(self.batch, value, frozen, {'tree': tree, 'sha': tree}, self.artifact, key)

    def initial(self):
        return self.ingest(verdict([finding('F1', blocking=True)], lineage=[{'local_id': 'F1', 'prior_id': None}]))

    def test_monotonic_explicit_lineage_renaming_and_ingestion_replay(self):
        first = self.initial()
        key, frozen = history.snapshot(self.batch, 'tree0')
        self.assertEqual(0, frozen['revision'])
        replay = history.ingest(self.batch, first['raw'], frozen, first['candidate'], self.artifact, key)
        self.assertIs(first, replay)
        renamed = finding('F97', blocking=True)
        renamed['title'] = 'A new title for the same concern'
        second = self.ingest(verdict([renamed], lineage=[{'local_id': 'F97', 'prior_id': 'R1'}],
                                    dispositions=[disposition()]), 'tree1')
        self.assertEqual({'F97': 'R1'}, second['lineage'])
        entry = history.ledger(self.batch)['findings']['R1']
        self.assertEqual('Finding F1', entry['original']['title'])
        self.assertEqual(2, len(entry['observations']))
        self.assertEqual(2, history.ledger(self.batch)['next_id'])

    def test_unknown_duplicates_omissions_and_contradictions_rejected(self):
        self.initial()
        _, frozen = history.snapshot(self.batch, 'tree1')
        good = verdict([finding('renamed', blocking=True)], lineage=[{'local_id': 'renamed', 'prior_id': 'R1'}],
                       dispositions=[disposition()])
        cases = []
        unknown = copy.deepcopy(good); unknown['finding_lineage'][0]['prior_id'] = 'R404'; cases.append(unknown)
        duplicate = copy.deepcopy(good); duplicate['finding_lineage'] *= 2; cases.append(duplicate)
        omitted = copy.deepcopy(good); omitted['previous_dispositions'] = []; cases.append(omitted)
        contradictory = copy.deepcopy(good); contradictory['previous_dispositions'][0]['status'] = 'resolved'; cases.append(contradictory)
        duplicate = copy.deepcopy(good); duplicate['previous_dispositions'] *= 2; cases.append(duplicate)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ApiError):
                history.validate(value, frozen)

    def test_rejected_history_retained_and_reopening_requires_new_evidence(self):
        self.initial()
        self.ingest(verdict(dispositions=[disposition(status='rejected')], assessment='passed'), 'tree1')
        _, frozen = history.snapshot(self.batch, 'tree2')
        old = finding('F1', blocking=True)
        value = verdict([old], lineage=[{'local_id': 'F1', 'prior_id': 'R1'}], dispositions=[disposition()])
        with self.assertRaises(ApiError):
            history.validate(value, frozen)
        value['findings'][0]['evidence'] = 'changed source at line 20'
        reopened = self.ingest(value, 'tree2')
        self.assertEqual(['R1'], reopened['disputes'])
        self.assertEqual('rejected', frozen['findings']['R1']['status'])

    def test_allocations_are_idempotent_and_only_targeted_completed_repairs_count(self):
        first = self.initial()
        cycle = history.allocate(self.batch, first, ['fix'])
        self.assertIs(cycle, history.allocate(self.batch, first, ['fix']))
        value = verdict([finding('F1', blocking=True), finding('F2', blocking=True)],
                lineage=[{'local_id': 'F1', 'prior_id': 'R1'}, {'local_id': 'F2', 'prior_id': None}],
                dispositions=[disposition()])
        second = self.ingest(value, 'tree1')
        self.assertEqual([1], history.ledger(self.batch)['findings']['R1']['survived_cycles'])
        self.assertEqual([], history.ledger(self.batch)['findings']['R2']['survived_cycles'])
        history.allocate(self.batch, second, ['fix'])
        value['assessment'] = 'incomplete'
        value['finding_lineage'][1]['prior_id'] = 'R2'
        value['previous_dispositions'].append(disposition('R2'))
        incomplete = self.ingest(value, 'tree2')
        self.assertFalse(incomplete['completed'])
        self.assertEqual('allocated', history.ledger(self.batch)['cycles'][-1]['status'])
        self.assertEqual([], incomplete['disputes'])
        value['assessment'] = 'changes_requested'
        complete = self.ingest(value, 'tree3')
        self.assertEqual(['R1'], complete['disputes'])
        self.assertEqual([2], history.ledger(self.batch)['findings']['R2']['survived_cycles'])

    def test_controls_reject_booleans_and_zero(self):
        for value in (True, False, 0, -1, 2.0, '3', None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                history.controls({'max_repair_cycles': value})
        history.controls({'max_repair_cycles': 3, 'retry_review_adjudication': False})
        command = history.resume_command(12, 'github.com/org/repo', 'repair_limit',
                                         {'repair_cycles_used': 3, 'max_repair_cycles': 1})
        self.assertTrue(command.endswith('--max-repair-cycles 4'))


class ContinuityEngineTest(ScriptedProviders, unittest.TestCase):
    workspace_name = test_issue_engine.IssueEngineTest.workspace_name
    setUp = test_issue_engine.IssueEngineTest.setUp
    fix = test_issue_engine.IssueEngineTest.fix
    start = test_issue_engine.IssueEngineTest.start
    last = test_issue_engine.IssueEngineTest.last

    def command(self, run, stage):
        if stage != 'adjudication':
            return super().command(run, stage)
        return ['/bin/sh', '-c', 'cat > "$ADJUDICATION_STDIN"; echo adjudication >> "$MARKS"; if [ "${ADJUDICATION_EDIT:-}" = yes ]; then echo changed >> source.txt; fi; cp "$ADJUDICATION_FILE" "$1"; printf "%s\\n" \'{"type":"turn.completed"}\'',
                'adjudication', str(run.stage_dir(stage) / 'result.json')]

    def adjudication(self, decision='unresolved', ids=('R1',)):
        file = self.root / 'adjudication-response.json'
        file.write_text(json.dumps({'decisions': [{'finding_id': key, 'decision': decision,
                'reason': 'Focused source inspection', 'evidence': 'candidate source at line 12'} for key in ids]}))
        os.environ['ADJUDICATION_FILE'] = str(file)
        os.environ['ADJUDICATION_STDIN'] = str(self.root / 'adjudication-stdin')
        self.addCleanup(os.environ.pop, 'ADJUDICATION_FILE', None)
        self.addCleanup(os.environ.pop, 'ADJUDICATION_STDIN', None)

    def blockers(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])

    def counts(self):
        lines = self.marks.read_text().splitlines() if self.marks.exists() else []
        return {'synthesis': sum(line.startswith('synthesis cwd=') for line in lines)}

    def adjudication_count(self):
        return sum(line == 'adjudication' for line in self.marks.read_text().splitlines())

    def conflict(self):
        self.blockers()
        self.adjudication()
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('review_conflict', caught.exception.code)
        self.assertEqual(3, len(self.turns))
        self.assertEqual(1, self.adjudication_count())
        return self.last()

    def test_three_repairs_then_limit_and_raise_without_repeating_completed_review(self):
        self.blockers()
        self.adjudication('uphold')
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('repair_limit', caught.exception.code)
        batch = self.last()
        self.assertEqual(3, len(history.ledger(batch)['cycles']))
        self.assertEqual(4, len(self.turns))
        self.assertEqual(1, self.adjudication_count())
        self.assertEqual(4, self.counts()['synthesis'])
        self.verdict('passed')
        result = self.start(batch=batch['id'], max_repair_cycles=4)
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(5, len(self.turns))
        self.assertEqual(5, self.counts()['synthesis'])
        self.assertEqual(4, len(history.ledger(self.last())['cycles']))

    def test_ordinary_conflict_resume_has_no_model_discovery_or_auth_calls(self):
        batch = self.conflict()
        counts = self.counts()
        with patch('pr_review.owner_providers.preflight', side_effect=AssertionError('auth')), \
             patch('pr_review.review_workspace.tool_version', side_effect=AssertionError('model discovery')):
            with self.assertRaises(ApiError) as caught:
                self.start(batch=batch['id'])
        self.assertEqual('review_conflict', caught.exception.code)
        self.assertEqual(counts, self.counts())
        self.assertEqual(1, self.adjudication_count())
        self.assertNotIn('final_tree', self.last())

    def test_explicit_retry_calls_only_adjudication_once_and_preserves_outcomes(self):
        batch = self.conflict()
        self.adjudication('reject')
        counts = self.counts()
        with patch('pr_review.owner_providers.preflight', side_effect=AssertionError('auth')), \
             patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')):
            result = self.f.start(batch=batch['id'], retry_review_adjudication=True)
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(counts, self.counts())
        self.assertEqual(2, self.adjudication_count())
        ledger = history.ledger(self.last())
        self.assertEqual('rejected', ledger['findings']['R1']['status'])
        self.assertEqual(2, len(ledger['rounds'][-1]['adjudications']))

    def test_reject_cannot_manufacture_missing_acceptance_outcomes(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')], outcomes=[])
        self.adjudication('reject')
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('review_conflict', caught.exception.code)
        self.assertNotIn('final_tree', self.last())
        self.assertIn('passed result needs', caught.exception.details['summary'])

    def test_saved_validated_response_replays_after_crash_without_second_call(self):
        self.blockers()
        self.adjudication('reject')
        from pr_review.owner_state import OwnerSession
        real_save = OwnerSession.save
        crashed = []
        def save(instance, state):
            real_save(instance, state)
            for record in (state.get('batches') or [{}])[-1].get('review_history', {}).get('rounds', []):
                for request in record.get('adjudications', {}).values():
                    if request.get('status') == 'validated' and not request.get('applied') and not crashed:
                        crashed.append(True)
                        raise RuntimeError('crash after durable response')
        with patch.object(OwnerSession, 'save', new=save), self.assertRaisesRegex(RuntimeError, 'durable response'):
            self.start()
        batch = self.last()
        saved = next(iter(history.ledger(batch)['rounds'][-1]['adjudications'].values()))
        context_path = Path(saved['artifact']).parent.parent / 'context/adjudication.json'
        input_bytes = context_path.read_bytes()
        counts = self.counts()
        result = self.start(batch=batch['id'])
        self.assertEqual(input_bytes, context_path.read_bytes())
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(1, self.adjudication_count())
        self.assertEqual(counts, self.counts())
        self.assertEqual(1, sum('adjudication' in d for d in history.ledger(self.last())['findings']['R1']['dispositions']))

    def test_base_and_lineage_invalid_output_gets_one_format_correction(self):
        self.result_file.write_text(json.dumps({'assessment': 'passed', 'finding_lineage': 'broken'}))
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('invalid_result', caught.exception.code)
        self.assertEqual(2, self.counts()['synthesis'])

    def test_legacy_batch_without_attempts_is_not_stamped_on_resume(self):
        state = self.session.load()
        # First model preflight failure creates an untouched batch.
        with patch('pr_review.owner_providers.preflight', side_effect=ApiError('model_failed', 'fixture')), \
             self.assertRaises(ApiError):
            self.start()
        state = self.session.load()
        batch = state['batches'][-1]
        batch.pop('review_control_version')
        batch.pop('review_history')
        batch.pop('max_repair_cycles')
        self.session.save(state)
        result = self.start(batch=batch['id'])
        self.assertNotIn('review_control_version', result['batches'][-1])
        with self.assertRaises(ApiError) as caught:
            self.start(batch=batch['id'], max_repair_cycles=2)
        self.assertEqual('invalid_request', caught.exception.code)

    def test_reopened_rejected_concern_bundles_once_with_persistent_blocker(self):
        self.adjudication('unresolved', ids=('R1', 'R2'))
        def fix(session, state, batch, specification, feedback):
            report = self.fix(session, state, batch, specification, feedback)
            first, second = finding('F1', blocking=True, source='new'), finding('F2', blocking=True, source='new')
            if len(self.turns) == 1:
                self.verdict('changes_requested', findings=[first, second])
            elif len(self.turns) == 2:
                self.verdict('changes_requested', findings=[second])
                value = json.loads(self.result_file.read_text())
                value.update(finding_lineage=[{'local_id': 'F2', 'prior_id': 'R2'}],
                             previous_dispositions=[disposition('R1', 'rejected'), disposition('R2')])
                self.result_file.write_text(json.dumps(value))
            else:
                first.update(id='renamed', title='Renamed concern', evidence='new regression at line 20')
                self.verdict('changes_requested', findings=[first, second])
                value = json.loads(self.result_file.read_text())
                value.update(finding_lineage=[{'local_id': 'renamed', 'prior_id': 'R1'}, {'local_id': 'F2', 'prior_id': 'R2'}],
                             previous_dispositions=[disposition('R1'), disposition('R2')])
                self.result_file.write_text(json.dumps(value))
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=fix), self.assertRaises(ApiError) as caught:
            self.f.start(issue_harness=True)
        self.assertEqual('review_conflict', caught.exception.code)
        self.assertEqual(['R1', 'R2'], caught.exception.details['disputed_findings'])
        self.assertEqual(1, self.adjudication_count())
        batch = self.last()
        last = history.ledger(batch)['rounds'][-1]
        frozen = history.ledger(batch)['synthesis_inputs'][last['input_key']]
        self.assertEqual('rejected', frozen['findings']['R1']['status'])
        self.assertEqual('R1', last['lineage']['renamed'])
        adapter = acceptance.IssueAdapter(self.session, self.session.load(), batch, {})
        self.assertNotIn('review_history', adapter.reviewer_context())
        self.assertNotIn('implementer_report', adapter.reviewer_context())
        self.assertIn('review_history', adapter.synthesis_context())
        self.assertIn('new regression at line 20', json.dumps(last['raw']))

    def test_crash_after_cycle_allocation_never_double_charges_or_repeats_review(self):
        from pr_review.owner_state import OwnerSession
        real_save = OwnerSession.save
        crashed = []
        self.blockers()
        def save(instance, state):
            real_save(instance, state)
            batches = state.get('batches') or []
            cycles = batches[-1].get('review_history', {}).get('cycles', []) if batches else []
            if cycles and cycles[-1]['status'] == 'allocated' and not crashed:
                crashed.append(True)
                raise RuntimeError('allocated crash')
        with patch.object(OwnerSession, 'save', new=save), self.assertRaisesRegex(RuntimeError, 'allocated crash'):
            self.start()
        batch = self.last()
        self.assertEqual(1, len(self.turns))
        self.assertEqual(1, len(history.ledger(batch)['cycles']))
        self.verdict('passed')
        result = self.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(2, len(self.turns))
        self.assertEqual(2, self.counts()['synthesis'])
        self.assertEqual(1, len(history.ledger(self.last())['cycles']))

    def test_saved_synthesis_ingestion_is_idempotent_after_crash(self):
        from pr_review.owner_state import OwnerSession
        real_save = OwnerSession.save
        crashed = []
        self.blockers()
        def save(instance, state):
            real_save(instance, state)
            batches = state.get('batches') or []
            rounds = batches[-1].get('review_history', {}).get('rounds', []) if batches else []
            if rounds and not crashed:
                crashed.append(True)
                raise RuntimeError('ingested crash')
        with patch.object(OwnerSession, 'save', new=save), self.assertRaisesRegex(RuntimeError, 'ingested crash'):
            self.start()
        batch = self.last()
        self.verdict('passed')
        result = self.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(2, self.counts()['synthesis'])  # cached first decision, then repaired candidate
        self.assertEqual(2, len(history.ledger(self.last())['rounds']))
        self.assertEqual(2, history.ledger(self.last())['next_id'])


    def test_retry_disposition_crash_resumes_authorized_stage_with_frozen_input(self):
        from pr_review.owner_state import OwnerSession
        batch = self.conflict()
        self.adjudication('reject')
        real_save = OwnerSession.save
        crashed = []
        def save(instance, state):
            real_save(instance, state)
            current = (state.get('batches') or [{}])[-1]
            for record in current.get('review_history', {}).get('rounds', []):
                for request in record.get('adjudications', {}).values():
                    if request['request']['generation'] == 1 and request.get('applied') and not crashed:
                        crashed.append(True)
                        raise RuntimeError('retry applied crash')
        with patch.object(OwnerSession, 'save', new=save), self.assertRaisesRegex(RuntimeError, 'retry applied crash'):
            self.start(batch=batch['id'], retry_review_adjudication=True)
        batch = self.last()
        self.assertTrue(history.ledger(batch)['rounds'][-1]['retry_pending'])
        saved = next(r for r in history.ledger(batch)['rounds'][-1]['adjudications'].values() if r['request']['generation'] == 1)
        context_path = Path(saved['artifact']).parent.parent / 'context/adjudication.json'
        input_bytes = context_path.read_bytes()
        with patch('pr_review.owner_providers.preflight', side_effect=AssertionError('auth')), \
             patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')):
            result = self.f.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(input_bytes, context_path.read_bytes())
        self.assertEqual(2, self.adjudication_count())
        entry = history.ledger(self.last())['findings']['R1']
        self.assertEqual(2, sum('adjudication' in d for d in entry['dispositions']))

    def test_retry_requires_saved_conflict_and_changed_candidate_gets_normal_review(self):
        with self.assertRaises(ApiError) as invalid:
            self.start(retry_review_adjudication=True)
        self.assertEqual('invalid_request', invalid.exception.code)
        untouched = self.last()
        # Continue that untouched versioned batch to a conflict.
        self.blockers()
        self.adjudication()
        with self.assertRaises(ApiError):
            self.start(batch=untouched['id'])
        batch = self.last()
        self.verdict('passed')
        (Path(self.session.load()['worktree']) / 'manual.txt').write_text('inspected manual change\n')
        before = self.counts()['synthesis']
        result = self.start(batch=batch['id'], adopt=True)
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(before + 1, self.counts()['synthesis'])
        self.assertEqual(1, self.adjudication_count())


    def test_retry_uphold_stops_before_repairs_then_normal_resume_uses_saved_decision(self):
        batch = self.conflict()
        self.adjudication('uphold')
        counts, turns = self.counts(), len(self.turns)
        with patch('pr_review.owner_providers.preflight', side_effect=AssertionError('auth')), \
             patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')), \
             self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'], retry_review_adjudication=True)
        self.assertEqual('resume_repair', caught.exception.details['next_action'])
        self.assertEqual('adjudication_complete', caught.exception.details['stop_reason'])
        self.assertEqual(counts, self.counts())
        self.assertEqual(turns, len(self.turns))
        self.assertEqual(2, self.adjudication_count())
        self.assertEqual(2, len(history.ledger(self.last())['cycles']))
        self.verdict('passed')
        result = self.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(2, self.adjudication_count())
        self.assertEqual(counts['synthesis'] + 1, self.counts()['synthesis'])
        self.assertEqual(turns + 1, len(self.turns))

    def test_retry_rejection_preserves_unrelated_blocker_and_does_not_repair(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new'),
                                                    finding('F2', blocking=True, source='new')])
        self.adjudication('unresolved', ids=('R1', 'R2'))
        with self.assertRaises(ApiError):
            self.start()
        batch = self.last()
        self.adjudication('reject', ids=('R1', 'R2'))
        path = Path(os.environ['ADJUDICATION_FILE'])
        value = json.loads(path.read_text())
        value['decisions'][1]['decision'] = 'uphold'
        path.write_text(json.dumps(value))
        with patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')), self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'], retry_review_adjudication=True)
        self.assertEqual('resume_repair', caught.exception.details['next_action'])
        batch = self.last()
        self.assertEqual(1, len(batch['harness_review']['findings']))
        self.assertIn('Finding F2', batch['harness_review']['findings'][0])
        self.assertEqual('rejected', history.ledger(batch)['findings']['R1']['status'])
        self.assertEqual('open', history.ledger(batch)['findings']['R2']['status'])
        self.assertEqual(3, len(self.turns))
        self.assertEqual(2, self.adjudication_count())


    def test_instruction_policy_changes_invalidate_context_identity(self):
        self.start()
        state, batch = self.session.load(), self.last()
        original = history.context_identity(self.session, state, batch)
        for name in ('synthesis-testing-focused.txt', 'synthesis-testing-full.txt', 'acceptance-issue-adjudication.txt'):
            with self.subTest(name=name), patch.dict(acceptance._PROMPTS, {name: acceptance._PROMPTS[name] + '\nChanged policy'}):
                self.assertNotEqual(original, history.context_identity(self.session, state, batch))


    def test_incomplete_verification_keeps_allocated_cycle_and_resume_reruns_only_synthesis(self):
        self.blockers()
        def fix(session, state, batch, specification, feedback):
            report = self.fix(session, state, batch, specification, feedback)
            if len(self.turns) == 2:
                value = json.loads(self.result_file.read_text())
                value['verification_blockers'] = [{'category': 'environment', 'command': 'focused-test',
                    'reason': 'daemon unavailable', 'manual_command': 'focused-test on a suitable host'}]
                self.result_file.write_text(json.dumps(value))
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=fix), self.assertRaises(ApiError) as caught:
            self.f.start(issue_harness=True)
        self.assertEqual('review_incomplete', caught.exception.code)
        batch = self.last()
        self.assertEqual('allocated', history.ledger(batch)['cycles'][-1]['status'])
        self.assertEqual([], history.ledger(batch)['findings']['R1']['survived_cycles'])
        self.verdict('passed')
        with patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')):
            result = self.f.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual('complete', history.ledger(self.last())['cycles'][-1]['status'])
        self.assertEqual(1, len(history.ledger(self.last())['cycles']))
        self.assertEqual(3, self.counts()['synthesis'])


    def test_unchanged_source_with_new_report_can_pass_third_review_before_no_progress_stop(self):
        def fix(session, state, batch, specification, feedback):
            self.turns.append(list(feedback))
            if len(self.turns) == 3:
                self.verdict('passed')
            else:
                self.blockers()
            return {'status': 'ready', 'summary': f'Candidate report {len(self.turns)}',
                    'checks': [f'new focused evidence {len(self.turns)}'], 'findings': [], 'questions': []}
        with patch('pr_review.issue_harness.invoke', side_effect=fix):
            result = self.f.start(issue_harness=True)
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(3, len(self.turns))
        self.assertEqual(3, self.counts()['synthesis'])
        self.assertEqual(0, self.adjudication_count())


    def test_completed_raw_adjudication_response_recovers_before_validation_save(self):
        self.blockers()
        self.adjudication('reject')
        real_response = acceptance.response
        crashed = []
        def response(choice, result, directory):
            if directory.name == 'adjudication' and not crashed:
                crashed.append(True)
                raise RuntimeError('raw response crash')
            return real_response(choice, result, directory)
        with patch('pr_review.acceptance.response', side_effect=response), self.assertRaisesRegex(RuntimeError, 'raw response crash'):
            self.start()
        batch = self.last()
        result = self.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(1, self.adjudication_count())
        self.assertEqual(3, self.counts()['synthesis'])


    def test_disposable_checkout_failure_never_recovers_its_raw_adjudication_response(self):
        self.blockers()
        self.adjudication('reject')
        with patch.dict(os.environ, {'ADJUDICATION_EDIT': 'yes'}), self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('review_checkout_modified', caught.exception.code)
        batch = self.last()
        result = self.start(batch=batch['id'])
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(2, self.adjudication_count())
        self.assertNotIn('integrity_failure', self.last())


class WorkflowStopTest(unittest.TestCase):
    def setUp(self):
        import test_issues
        from pr_review import issues
        self.dispatch = issues.dispatch
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.request = self.t.terminal_request('--yolo')

    def stop(self):
        def review(session, state, batch, specification):
            key, frozen = history.snapshot(batch, history.context_identity(session, state, batch))
            f = finding('F1', blocking=True, source='new')
            raw = verdict([f], lineage=[{'local_id': 'F1', 'prior_id': None}])
            raw['outcomes'] = [{'item': key, 'status': 'fixed', 'evidence': 'Inspected'} for key in batch['items']]
            artifact = session.path / 'fixture-synthesis.json'
            artifact.write_text(json.dumps(raw))
            candidate = owner_git.review_commit(session, batch['checkpoint']['tree'], batch['baseline'])
            record = history.ingest(batch, raw, frozen, candidate, artifact, key)
            record['disputes'] = ['R1']
            batch['review_round'] = record['id']
            history.stop(batch, 'review_conflict', record, 'Fixture unresolved disagreement',
                         context=history.context_identity(session, state, batch))
        with patch('pr_review.acceptance.review_issue', side_effect=review):
            result = self.dispatch(self.request)
        self.assertEqual('review_conflict', result['error']['code'], result)
        self.assertEqual('inspect_review_conflict', result['next_action'])
        self.assertEqual('adjudication', result['stage'])
        self.assertEqual('review_conflict', result['stop_reason'])
        return result

    def test_workflow_unchanged_resume_precedes_discovery_auth_and_deduplicates_comment(self):
        before = self.stop()
        comments = copy.deepcopy(self.t.f['comments'])
        with patch('pr_review.models.discover', side_effect=AssertionError('discovery')), \
             patch('pr_review.owner_providers.preflight', side_effect=AssertionError('auth')), \
             patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation')), \
             patch('pr_review.issues.assess_issue', side_effect=AssertionError('planning')):
            after = self.dispatch(self.request)
        self.assertEqual('review_conflict', after['error']['code'])
        self.assertEqual(comments, self.t.f['comments'])
        status = next(c['body'] for c in comments if 'fix-issue stopped: review conflict' in c['body'])
        for fragment in ('R1', 'Repair cycles: 0/3', 'Local worktree:', 'Artifact:', '--retry-review-adjudication', 'local and unaccepted'):
            self.assertIn(fragment, status)
        self.assertEqual(before['worktree'], after['worktree'])

    def test_changed_discussion_reaches_normal_reassessment_and_rejects_obsolete_retry(self):
        self.stop()
        self.t.f['issue']['body'] += '\nNew acceptance requirement'
        retry = self.dispatch({**self.request, 'retry_review_adjudication': True})
        self.assertEqual('invalid_request', retry['error']['code'])
        count = len(self.t.assessments)
        with patch('pr_review.issues.Workflow.execute_approved', return_value={'outcome': 'resumed'}):
            result = self.dispatch(self.request)
        self.assertGreater(len(self.t.assessments), count)
        self.assertNotEqual('review_conflict', result.get('error', {}).get('code'))

    def test_changed_pr_feedback_reaches_normal_reassessment(self):
        self.stop()
        self.t.feedback.append({'id': 701, 'body': 'New required behavior', 'user': {'login': 'reviewer'},
                                'created_at': '2026-11-04T00:00:00Z'})
        count = len(self.t.assessments)
        with patch('pr_review.issues.Workflow.execute_approved', return_value={'outcome': 'resumed'}):
            result = self.dispatch(self.request)
        self.assertGreater(len(self.t.assessments), count)
        self.assertNotEqual('review_conflict', result.get('error', {}).get('code'))


class ControlCliTest(unittest.TestCase):
    def test_cli_controls_are_execution_only_and_omission_preserves_limit(self):
        from pr_review.issue_cli import parser
        from pr_review.issues import configuration_identity
        from pr_review import settings
        args = vars(parser().parse_args(['-i', '12']))
        self.assertNotIn('max_repair_cycles', args)
        explicit = vars(parser().parse_args(['-i', '12', '--max-repair-cycles', '5', '--retry-review-adjudication']))
        self.assertEqual(5, explicit['max_repair_cycles'])
        self.assertTrue(explicit['retry_review_adjudication'])
        self.assertEqual(configuration_identity(settings.initial_settings({})),
                         configuration_identity(settings.initial_settings(explicit)))
