"""The issue coordinator must not recreate the harness's coding loop."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_issue_steps
from pr_review import issue_harness, owner_git, owner_contracts


class ArgumentTest(unittest.TestCase):
    def test_implementation_resumes_explicit_session_but_review_is_fresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / 'schema.json'
            schema.write_text(json.dumps(issue_harness.SCHEMA))
            for provider in ('codex', 'claude'):
                choice = {'provider': provider, 'model': 'chosen', 'effort': 'high'}
                session = {'session_id': '00000000-0000-4000-8000-000000000001', 'started': True}
                args = issue_harness.arguments(choice, 'fix', root, schema, session)
                self.assertIn(session['session_id'], args)
                self.assertNotIn('--last', args)
                self.assertNotIn('--ephemeral', args)
                self.assertNotIn('--no-session-persistence', args)
                review = issue_harness.arguments(choice, 'review', root, schema, {})
                self.assertNotIn(session['session_id'], review)
                self.assertIn('--ephemeral' if provider == 'codex' else '--no-session-persistence', review)


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.calls = []

    def report(self, status='ready', findings=None):
        return {'status': status, 'summary': 'Candidate assessment',
                'outcomes': [{'item': self.f.key, 'status': 'fixed', 'evidence': 'Checked candidate'}], 'checks': ['Relevant test passed'],
                'findings': findings or [], 'questions': []}

    def model(self, session, state, batch, role, specification, feedback):
        self.calls.append((role, feedback))
        if role == 'fix':
            (Path(state['worktree']) / 'implementation.txt').write_text(str(len(self.calls)))
            batch['checkpoint'] = owner_git.checkpoint(session, state, 'test harness')
        return self.report()

    def test_complete_candidate_has_one_independent_review_and_commit(self):
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(issue_harness=True, auto_commit=True)
        batch = view['batches'][-1]
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual('ready', batch['status'])
        self.assertTrue(batch['commit_progress']['complete'])
        self.assertTrue(owner_contracts.current(batch))
        batch['final_tree'] = 'changed'
        self.assertFalse(owner_contracts.current(batch))

    def test_partial_work_continues_without_review_or_test_replay(self):
        def model(*args):
            result = self.model(*args)
            if len(self.calls) == 1:
                result['status'] = 'working'
            return result
        with patch('pr_review.issue_harness.invoke', side_effect=model), \
                patch('pr_review.owner_providers.invoke', side_effect=AssertionError('No legacy task pipeline')):
            view = self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_review_findings_return_to_implementation(self):
        def model(*args):
            result = self.model(*args)
            if len(self.calls) == 2:
                result = self.report('changes_requested', ['Acceptance regression'])
            return result
        with patch('pr_review.issue_harness.invoke', side_effect=model):
            view = self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual(['Acceptance regression'], self.calls[2][1])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_legacy_interruption_preserves_completed_commit_boundary(self):
        self.f.blocked_task = 'pipeline'
        view = self.f.start()
        old = view['batches'][-1]
        completed = old['tasks'][0]
        state = self.f.owner.load()
        state['batches'][-1]['tasks'][-1]['status'] = 'repairing'
        self.f.owner.save(state)
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            result = self.f.start(batch=old['id'], issue_harness=True, auto_commit=True)
        batch = result['batches'][-1]
        self.assertEqual(completed, batch['tasks'][0])
        self.assertEqual('issue_harness', batch['plans'][-1]['migration'])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        self.assertTrue(owner_contracts.current(batch))

    def test_invalid_completion_report_is_corrected_without_task_replanning(self):
        from pr_review.common import ApiError
        invoked = 0
        def model(*args):
            nonlocal invoked
            invoked += 1
            if invoked == 1:
                raise ApiError('invalid_result', 'Missing summary')
            return self.model(*args)
        with patch('pr_review.issue_harness.invoke', side_effect=model):
            result = self.f.start(issue_harness=True)
        self.assertEqual(3, invoked)
        self.assertIn('Correct the completion report', self.calls[0][1][-1])
        self.assertEqual('ready', result['batches'][-1]['status'])

    def test_review_requires_evidence_for_each_feedback_item(self):
        from pr_review.common import ApiError
        result = self.report()
        issue_harness.validate(result, 'review', [self.f.key])
        result['outcomes'] = []
        with self.assertRaisesRegex(ApiError, 'each selected feedback item'):
            issue_harness.validate(result, 'review', [self.f.key])


    def stop_in_review(self):
        from pr_review.common import ApiError
        def model(*args):
            if args[3] == 'review':
                raise ApiError('model_failed', 'Review interrupted')
            return self.model(*args)
        with patch('pr_review.issue_harness.invoke', side_effect=model), self.assertRaises(ApiError):
            self.f.start(issue_harness=True)
        batch = self.f.owner.load()['batches'][-1]
        self.assertEqual('review', batch['harness_phase'])
        self.assertEqual([], batch['tasks'])
        self.calls.clear()
        return batch

    def test_interrupted_review_resumes_review_only_by_saved_engine(self):
        batch = self.stop_in_review()
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(batch=batch['id'])
        self.assertEqual(['review'], [r for r, _ in self.calls])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_early_harness_synthetic_task_cannot_restart_implementation(self):
        batch = self.stop_in_review()
        state = self.f.owner.load()
        state['batches'][-1]['tasks'] = [{'id': 'approved-specification', 'status': 'fixing',
            'before': batch['harness_before'], 'items': batch['items']}]
        self.f.owner.save(state)
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(batch=batch['id'])
        self.assertEqual(['review'], [r for r, _ in self.calls])
        self.assertEqual(1, len(view['batches'][-1]['tasks']))

    def test_adopted_source_invalidates_candidate_and_unadopted_source_blocks(self):
        from pr_review.common import ApiError
        batch = self.stop_in_review()
        state = self.f.owner.load()
        (Path(state['worktree']) / 'retained.txt').write_text('External edit')
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaisesRegex(ApiError, 'adopt'):
            self.f.start(batch=batch['id'])
        invoke.assert_not_called()
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(batch=batch['id'], adopt=True)
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertTrue(owner_contracts.current(view['batches'][-1]))

    def test_partial_continuation_preserves_findings_for_independent_review(self):
        def model(*args):
            result = self.model(*args)
            if len(self.calls) == 2:
                return self.report('changes_requested', ['Original correctness finding'])
            if len(self.calls) == 3:
                result['status'] = 'working'
            return result
        with patch('pr_review.issue_harness.invoke', side_effect=model):
            self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review', 'fix', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual(['Original correctness finding'], self.calls[-1][1])
        self.assertIn('Continue the approved', self.calls[-2][1][-1])

    def test_ready_candidate_resets_consecutive_no_progress_counter(self):
        sequence = ['working', 'working', 'ready', 'changes_requested', 'working', 'ready', 'ready']
        def model(*args):
            self.calls.append((args[3], args[-1]))
            status = sequence[len(self.calls)-1]
            return self.report(status, ['Concrete defect'] if status == 'changes_requested' else [])
        with patch('pr_review.issue_harness.invoke', side_effect=model):
            view = self.f.start(issue_harness=True)
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(7, len(self.calls))
        self.assertEqual(0, view['batches'][-1]['harness_unchanged'])

    def test_three_identical_rejections_stop(self):
        from pr_review.common import ApiError
        def model(*args):
            self.calls.append((args[3], args[-1]))
            return self.report('changes_requested', ['Concrete defect']) if args[3] == 'review' else self.report()
        with patch('pr_review.issue_harness.invoke', side_effect=model), self.assertRaisesRegex(ApiError, 'same unchanged'):
            self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review'] * 3, [r for r, _ in self.calls])

    def test_legacy_partial_edits_can_be_adopted_without_losing_completed_boundary(self):
        from pr_review.common import ApiError
        self.f.blocked_task = 'pipeline'
        view = self.f.start()
        batch = view['batches'][-1]
        completed = batch['tasks'][0]
        (Path(view['worktree']) / 'partial.txt').write_text('Interrupted old fixer')
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaises(ApiError):
            self.f.start(batch=batch['id'], issue_harness=True)
        invoke.assert_not_called()
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            result = self.f.start(batch=batch['id'], issue_harness=True, adopt=True, auto_commit=True)
        migrated = result['batches'][-1]
        self.assertEqual(completed, migrated['tasks'][0])
        self.assertEqual(batch['baseline'], migrated['baseline'])
        self.assertEqual(2, len(migrated['commit_progress']['commits']))
        self.assertEqual(completed['after']['tree'], migrated['commit_plan']['units'][0]['tree'])
        self.assertEqual('Interrupted old fixer', (Path(view['worktree']) / 'partial.txt').read_text())


    def test_review_question_answer_resumes_without_replan(self):
        from pr_review import owner_import
        def question(*args):
            value = self.model(*args)
            if args[3] == 'review':
                value.update(status='needs_input', questions=['Keep archived records searchable?'])
            return value
        with patch('pr_review.issue_harness.invoke', side_effect=question):
            view = self.f.start(issue_harness=True)
        batch = view['batches'][-1]
        self.assertEqual('paused', batch['status'])
        state = self.f.owner.load()
        item = next(i for i in state['feedback'] if i['kind'] == 'issue_plan')
        owner_import.from_issue(self.f.owner, item['source']['plan'],
            {'title': item['title'], 'html_url': item['source']['issue']}, 'workflow',
            {'answers': ['Keep them searchable.']})
        self.calls.clear()
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(batch=batch['id'])
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertNotIn('harness_questions', view['batches'][-1])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_changed_scope_preserves_completed_migration_boundaries(self):
        from pr_review import owner_import
        from pr_review.common import ApiError
        self.f.blocked_task = 'pipeline'
        old = self.f.start()['batches'][-1]
        completed = old['tasks'][0]
        def interrupted(*args):
            if args[3] == 'review':
                raise ApiError('model_failed', 'Review interrupted')
            return self.model(*args)
        with patch('pr_review.issue_harness.invoke', side_effect=interrupted), self.assertRaises(ApiError):
            self.f.start(batch=old['id'], issue_harness=True)
        state = self.f.owner.load()
        item = next(i for i in state['feedback'] if i['kind'] == 'issue_plan')
        owner_import.from_issue(self.f.owner, {**item['source']['plan'], 'revision': 2, 'body': 'Revised approved scope'},
            {'title': item['title'], 'html_url': item['source']['issue']}, 'workflow')
        with patch('pr_review.issue_harness.invoke', side_effect=self.model):
            view = self.f.start(batch=old['id'], auto_commit=True)
        batch = view['batches'][-1]
        self.assertEqual(completed, batch['tasks'][0])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        self.assertTrue(all(t['status'] == 'complete' for t in batch['tasks']))


    def test_legacy_final_review_without_edits_preserves_task_commits(self):
        view = self.f.start()
        state = self.f.owner.load()
        old = state['batches'][-1]
        old['status'] = 'paused'
        old['review']['complete'] = False
        tasks = list(old['tasks'])
        self.f.owner.save(state)
        with patch('pr_review.issue_harness.invoke', return_value=self.report()):
            result = self.f.start(batch=old['id'], issue_harness=True, auto_commit=True)
        batch = result['batches'][-1]
        self.assertEqual(tasks, batch['tasks'])
        self.assertEqual(2, len(batch['commit_progress']['commits']))
        self.assertEqual('tasks', batch['commit_plan']['mode'])



# Capture the real adapter before fixtures patch provider boundaries.
REAL_INVOKE = issue_harness.invoke


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.start(issue_harness=True)
        self.session = self.f.owner
        self.state = self.session.load()
        self.batch = self.state['batches'][-1]
        self.spec = next(i['source']['plan'] for i in self.state['feedback'] if i['kind'] == 'issue_plan')
        self.commands = []
        self.identity = '00000000-0000-4000-8000-000000000001'

    def transport(self, argv, **kwargs):
        from types import SimpleNamespace
        self.commands.append(argv)
        provider = argv[0]
        event = {'type': 'thread.started', 'thread_id': self.identity} if provider == 'codex' else {
            'type': 'system', 'subtype': 'init', 'session_id': self.identity}
        kwargs['on_line']('stdout', json.dumps(event))
        # Identity is durable before a long-running model finishes.
        saved = self.session.load()['issue_harness_sessions']
        self.assertEqual(self.identity, next(iter(saved.values()))['session_id'])
        value = {'status': 'ready', 'summary': 'Tested complete candidate',
                 'checks': ['Focused check passed'], 'questions': [], 'findings': []}
        Path(kwargs['stdout_path']).with_name('result.json').write_text(json.dumps(value))
        return SimpleNamespace(returncode=0, events=[{'type': 'turn.completed'}] if provider == 'codex' else [
            {'type': 'result', 'subtype': 'success', 'structured_output': value}])

    def invoke(self, role='fix'):
        return REAL_INVOKE(self.session, self.state, self.batch, role, self.spec, [])

    def test_exact_codex_session_is_saved_and_reused(self):
        self.batch['settings']['fix']['provider'] = 'codex'
        with patch('pr_review.issue_harness.run', side_effect=self.transport):
            self.invoke()
            self.invoke()
        self.assertNotIn('resume', self.commands[0])
        self.assertEqual(['codex', 'exec', 'resume'], self.commands[1][:3])
        self.assertEqual([self.identity, '-'], self.commands[1][-2:])

    def test_claude_session_is_saved_and_reused(self):
        self.batch['settings']['fix']['provider'] = 'claude'
        from uuid import UUID
        with patch('pr_review.issue_harness.uuid.uuid4', return_value=UUID(self.identity)), \
                patch('pr_review.issue_harness.run', side_effect=self.transport):
            self.invoke()
        with patch('pr_review.issue_harness.run', side_effect=self.transport):
            self.invoke()
        self.assertIn('--session-id', self.commands[0])
        self.assertIn('--resume', self.commands[1])

    def test_interruption_records_partial_tree_and_session_before_propagating(self):
        from pr_review.common import ApiError
        self.batch['settings']['fix']['provider'] = 'codex'
        before = self.batch['checkpoint']['tree']
        def interrupted(argv, **kwargs):
            self.transport(argv, **kwargs)
            (Path(self.state['worktree']) / 'partial.txt').write_text('unfinished implementation\n')
            raise ApiError('resource_limit', 'Runtime expired')
        with patch('pr_review.issue_harness.run', side_effect=interrupted), self.assertRaisesRegex(ApiError, 'Runtime expired'):
            self.invoke()
        saved = self.session.load()['batches'][-1]
        self.assertNotEqual(before, saved['checkpoint']['tree'])
        self.assertEqual(saved['checkpoint'], saved['attempts'][-1]['after'])
        self.assertEqual('failed', saved['attempts'][-1]['status'])
        self.assertTrue(self.session.load()['issue_harness_sessions'])

    def test_readonly_review_edit_invalidates_publication(self):
        from pr_review.common import ApiError
        def edited(argv, **kwargs):
            schema = json.loads(Path(kwargs['stdout_path']).with_name('schema.json').read_text())
            self.assertEqual(['fixed', 'already_addressed', 'no_change'],
                             schema['properties']['outcomes']['items']['properties']['status']['enum'])
            from types import SimpleNamespace
            (Path(self.state['worktree']) / 'unwanted.txt').write_text('review edit\n')
            return SimpleNamespace(returncode=0, events=[])
        with patch('pr_review.issue_harness.run', side_effect=edited), self.assertRaisesRegex(ApiError, 'protected source'):
            self.invoke('review')
        self.assertTrue(self.session.load()['batches'][-1]['integrity_failure'])

    def test_unrecorded_edit_prevents_provider_launch(self):
        from pr_review.common import ApiError
        (Path(self.state['worktree']) / 'manual.txt').write_text('human change\n')
        with patch('pr_review.issue_harness.run') as launch, self.assertRaisesRegex(ApiError, 'between harness turns'):
            self.invoke()
        launch.assert_not_called()

    def test_wrong_resumed_session_is_rejected(self):
        from pr_review.common import ApiError
        self.batch['settings']['fix']['provider'] = 'codex'
        with patch('pr_review.issue_harness.run', side_effect=self.transport):
            self.invoke()
            self.identity = '00000000-0000-4000-8000-000000000002'
            with self.assertRaisesRegex(ApiError, 'different implementation session'):
                self.invoke()

    def test_changed_specification_starts_its_own_session(self):
        self.batch['settings']['fix']['provider'] = 'codex'
        with patch('pr_review.issue_harness.run', side_effect=self.transport):
            self.invoke()
            self.spec = {**self.spec, 'body': 'Revised approved behavior'}
            self.invoke()
        self.assertNotIn('resume', self.commands[-1])
        self.assertEqual(2, len(self.session.load()['issue_harness_sessions']))
