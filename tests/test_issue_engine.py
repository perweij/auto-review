"""fix-issue acceptance review through the real shared engine with scripted providers.

The implementation turn is a test double; the review is the real engine, real Git
checkouts and the unchanged Owner commit guards.
"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_issue_steps
from engine_fixture import ScriptedProviders, finding
from pr_review import owner_contracts, owner_git
from pr_review.common import ApiError, read_json


class IssueEngineTest(ScriptedProviders, unittest.TestCase):
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
        # One reviewer matching synthesis: the direct path, no bubblewrap.
        state['settings']['final_review'] = {'reviewer_slots': [{'id': 'reviewer-1', **self.SYNTHESIS}],
                                             'synthesis': dict(self.SYNTHESIS), 'synthesis_tests': 'focused'}
        self.session.save(state)
        self.turns = []
        self.verdict()

    def fix(self, session, state, batch, specification, feedback):
        os.environ['OWNER_WORKTREE'] = state['worktree']
        self.turns.append(list(feedback))
        (Path(state['worktree']) / 'implementation.txt').write_text(f'turn {len(self.turns)}\n')
        batch['checkpoint'] = owner_git.checkpoint(session, state, 'test harness')
        return {'status': 'ready', 'summary': 'Candidate complete', 'checks': ['focused test passed'],
                'findings': [], 'questions': []}

    def start(self, **fields):
        with patch('pr_review.issue_harness.invoke', side_effect=self.fix):
            return self.f.start(issue_harness=True, **fields)

    def last(self):
        batch = self.session.load()['batches'][-1]
        self.state, self.batch = self.session.load(), batch
        return batch

    def artifact(self):
        return json.loads((self.workspace() / 'reviews/synthesis.json').read_text())

    def test_pass_with_observations_projects_to_the_unchanged_proof_and_passes_the_commit_guard(self):
        self.verdict(findings=[finding('F1', blocking=False, validity='UNVERIFIED', source='new')])
        view = self.start(auto_commit=True)
        batch = view['batches'][-1]
        self.last()
        # The engine artifact keeps the observation ...
        self.assertEqual(['F1'], [f['id'] for f in self.artifact()['findings']])
        # ... the legacy proof does not ...
        self.assertEqual('ready', batch['harness_review']['status'])
        self.assertEqual(([], []), (batch['harness_review']['findings'], batch['harness_review']['questions']))
        self.assertNotIn('harness_feedback', batch)
        self.assertEqual(1, len(self.turns))
        # ... and the unchanged guards accept the candidate and commit it.
        self.assertTrue(owner_contracts.current(batch))
        self.assertTrue(batch['commit_progress']['complete'])
        prompt = self.stdin_copy.read_text()
        self.assertIn('claims to assess, not\nevidence', prompt)
        self.assertNotIn('implementation_test_evidence', prompt)
        kinds = [a['kind'] for a in batch['attempts']]
        self.assertEqual(['review_engine'], [k for k in kinds if k == 'review_engine'])
        attempt = next(a for a in batch['attempts'] if a['kind'] == 'review_engine')
        self.assertEqual(('acceptance', 'complete'), (attempt['role'], attempt['status']))
        self.assertTrue(Path(attempt['result']).is_file())

    def test_a_mixed_result_returns_only_the_blocking_finding_to_the_implementation_session(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new', severity='High'),
                                                    finding('F2', blocking=False, source='new')])
        def fix(session, state, batch, specification, feedback):
            report = self.fix(session, state, batch, specification, feedback)
            if len(self.turns) == 2:                   # after the repair the candidate passes
                self.verdict('passed', findings=[finding('F2', blocking=False, source='new')])
            return report
        with patch('pr_review.issue_harness.invoke', side_effect=fix):
            view = self.f.start(issue_harness=True)
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(2, len(self.turns))
        feedback = self.turns[1]
        self.assertEqual(1, len(feedback))
        self.assertIn('Finding F1', feedback[0])
        self.assertNotIn('Finding F2', feedback[0])
        self.assertTrue(owner_contracts.current(self.last()))

    def test_incomplete_review_stops_and_resume_reruns_only_the_stage_without_evidence(self):
        self.verdict('incomplete', summary='The environment could not run the acceptance tests')
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('review_incomplete', caught.exception.code)
        batch = self.last()
        self.assertEqual('review', batch['harness_phase'])
        for field in ('harness_review', 'final_tree', 'verified_items'):
            self.assertNotIn(field, batch)
        self.assertNotIn('harness_repeated_rejection', batch)
        self.assertEqual(1, len(self.turns))
        self.verdict('passed')
        with patch('pr_review.issue_harness.invoke', side_effect=AssertionError('implementation is not rerun')):
            view = self.f.start(batch=batch['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(2, self.counts()['synthesis'])
        self.assertTrue(owner_contracts.current(self.last()))

    def test_needs_input_pauses_with_the_questions(self):
        self.verdict('needs_input', questions=['Keep archived records searchable?'])
        view = self.start()
        batch = view['batches'][-1]
        self.assertEqual('paused', batch['status'])
        self.assertEqual(['Keep archived records searchable?'], batch['harness_questions'])
        self.assertEqual('needs_input', batch['harness_review']['status'])

    def test_specification_and_feedback_coverage_is_enforced(self):
        self.verdict(outcomes=[])
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('invalid_result', caught.exception.code)
        self.assertNotIn('final_tree', self.last())
        self.assertEqual(2, self.counts()['synthesis'])        # one correction attempt
        self.verdict(outcomes=[{'item': 'issue:unknown', 'status': 'fixed', 'evidence': 'not a selected item'}])
        with self.assertRaises(ApiError):
            self.f.start(batch=self.last()['id'])

    def test_three_identical_rejections_stop(self):
        self.verdict('changes_requested', findings=[finding('F1', blocking=True, source='new')])
        def fix(session, state, batch, specification, feedback):
            self.turns.append(list(feedback))
            return {'status': 'ready', 'summary': 'No further change', 'checks': [], 'findings': [], 'questions': []}
        with patch('pr_review.issue_harness.invoke', side_effect=fix), self.assertRaisesRegex(ApiError, 'same unchanged'):
            self.f.start(issue_harness=True)
        self.assertEqual(3, len(self.turns))

    def test_changing_the_source_during_review_blocks_acceptance_until_adopted(self):
        with patch.dict(os.environ, {'MISBEHAVE': 'live'}), self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual('integrity_failed', caught.exception.code)
        batch = self.last()
        self.assertTrue(batch['integrity_failure'])
        for field in ('harness_review', 'final_tree', 'verified_items'):
            self.assertNotIn(field, batch)
        with patch('pr_review.issue_harness.invoke', side_effect=AssertionError('blocked until adopted')), \
                self.assertRaises(ApiError) as again:
            self.f.start(batch=batch['id'])
        self.assertEqual('integrity_failed', again.exception.code)
        view = self.start_resume_adopted(batch['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def start_resume_adopted(self, identifier):
        with patch('pr_review.issue_harness.invoke', side_effect=self.fix):
            return self.f.start(batch=identifier, adopt=True)

    def test_an_earlier_acceptance_is_cleared_before_a_new_candidate_is_reviewed(self):
        self.verdict(findings=[finding('F1', blocking=False, source='new')])
        self.start()
        batch = self.last()
        self.assertEqual('ready', batch['harness_review']['status'])
        from pr_review import acceptance
        stale = self.session.load()
        stale['batches'][-1].update(final_tree='stale', harness_review_tree='stale', verified_items=['x'])
        self.session.save(stale)
        state = self.session.load()
        seen = {}
        def spy(session, state, batch, adapter, *, role):
            seen.update({k: k in batch for k in ('harness_review', 'final_tree', 'verified_items', 'review_tree')})
            raise ApiError('model_failed', 'stop after observing the cleared proof')
        with patch('pr_review.acceptance.execute', side_effect=spy), self.assertRaises(ApiError):
            acceptance.review_issue(self.session, state, state['batches'][-1], {'revision': 1})
        self.assertEqual({k: False for k in seen}, seen)


if __name__ == '__main__':
    unittest.main()
