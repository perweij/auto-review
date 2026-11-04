"""The issue coordinator must not recreate the harness's coding loop."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import engine_stub
import test_issue_steps
from pr_review import issue_harness, owner_git, owner_contracts


class ArgumentTest(unittest.TestCase):
    def test_implementation_resumes_its_explicit_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / 'schema.json'
            schema.write_text(json.dumps(issue_harness.SCHEMA))
            for provider in ('codex', 'claude'):
                choice = {'provider': provider, 'model': 'chosen', 'effort': 'high'}
                session = {'session_id': '00000000-0000-4000-8000-000000000001', 'started': True}
                args = issue_harness.arguments(choice, root, schema, session)
                self.assertIn(session['session_id'], args)
                self.assertNotIn('--last', args)
                self.assertNotIn('--ephemeral', args)
                self.assertNotIn('--no-session-persistence', args)

    def test_acceptance_review_is_not_a_harness_role(self):
        self.assertFalse(hasattr(issue_harness, 'report') and 'role' in issue_harness.report.__code__.co_varnames)
        self.assertEqual(('session', 'state', 'batch', 'specification', 'feedback'),
                         issue_harness.invoke.__code__.co_varnames[:5])


class ExecutionTest(unittest.TestCase):
    """Implementation turns come from the harness; acceptance review comes from the shared engine."""
    def setUp(self):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.calls = []

    def report(self, status='ready', findings=None):
        return {'status': status, 'summary': 'Candidate assessment',
                'outcomes': [{'item': self.f.key, 'status': 'fixed', 'evidence': 'Checked candidate'}], 'checks': ['Relevant test passed'],
                'findings': findings or [], 'questions': []}

    def fix(self, session, state, batch, specification, feedback):
        self.calls.append(('fix', feedback))
        (Path(state['worktree']) / 'implementation.txt').write_text(str(len(self.calls)))
        batch['checkpoint'] = owner_git.checkpoint(session, state, 'test harness')
        return self.report()

    def review(self, session, state, batch, specification):
        self.calls.append(('review', None))
        return self.report()

    @contextlib.contextmanager
    def engines(self, fix=None, review=None, no_legacy=False):
        with patch('pr_review.issue_harness.invoke', side_effect=fix or self.fix), \
                patch('pr_review.acceptance.review_issue', side_effect=engine_stub.issue_review_from(review or self.review)):
            if no_legacy:
                with patch('pr_review.owner_providers.invoke', side_effect=AssertionError('No legacy task pipeline')):
                    yield
            else:
                yield

    def test_complete_candidate_has_one_engine_review_and_commit(self):
        with self.engines():
            view = self.f.start(issue_harness=True, auto_commit=True)
        batch = view['batches'][-1]
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual('ready', batch['status'])
        self.assertEqual(1, batch['review_engine'])
        self.assertTrue(batch['commit_progress']['complete'])
        self.assertTrue(owner_contracts.current(batch))
        batch['final_tree'] = 'changed'
        self.assertFalse(owner_contracts.current(batch))

    def test_partial_work_continues_without_review_or_test_replay(self):
        def fix(*args):
            result = self.fix(*args)
            if len(self.calls) == 1:
                result['status'] = 'working'
            return result
        with self.engines(fix=fix, no_legacy=True):
            view = self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_review_findings_return_to_implementation(self):
        def review(*args):
            self.calls.append(('review', None))
            return self.report('changes_requested', ['Acceptance regression']) if len(self.calls) == 2 else self.report()
        with self.engines(review=review):
            view = self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual(['Acceptance regression'], self.calls[2][1])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_interactive_owner_batch_cannot_resume_as_issue_harness(self):
        from pr_review.common import ApiError
        self.f.blocked_task = 'pipeline'
        self.f.start()
        before = self.f.owner.load()['batches'][-1]
        with patch('pr_review.issue_harness.invoke') as invoke:
            for adopt in (False, True):
                with self.assertRaisesRegex(ApiError, 'retired task pipeline') as caught:
                    self.f.start(batch=before['id'], issue_harness=True, adopt=adopt)
                self.assertEqual('unsupported_session', caught.exception.code)
                self.assertEqual(before, self.f.owner.load()['batches'][-1])
            invoke.assert_not_called()

    def test_old_synthetic_task_is_rejected_without_restarting_implementation(self):
        from pr_review.common import ApiError
        batch = self.stop_in_review()
        state = self.f.owner.load()
        state['batches'][-1]['tasks'] = [{'id': 'approved-specification', 'status': 'fixing',
            'items': batch['items'], 'before': batch['harness_before']}]
        self.f.owner.save(state)
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaisesRegex(ApiError, 'retired task pipeline'):
            self.f.start(batch=batch['id'])
        invoke.assert_not_called()

    def test_unfinished_batch_without_the_engine_marker_stops_but_keeps_its_artifacts(self):
        from pr_review.common import ApiError
        batch = self.stop_in_review()
        state = self.f.owner.load()
        state['batches'][-1].pop('review_engine')
        state['batches'][-1]['attempts'] = [{'role': 'fix', 'kind': 'issue_harness', 'status': 'complete'}]
        self.f.owner.save(state)
        before = self.f.owner.load()['batches'][-1]
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'])
        self.assertEqual('unsupported_session', caught.exception.code)
        invoke.assert_not_called()
        self.assertEqual(before, self.f.owner.load()['batches'][-1])
        self.assertTrue(Path(self.f.owner.load()['worktree']).is_dir())

    def test_untouched_unmarked_batch_adopts_the_marker_and_current_settings(self):
        from pr_review import issue_harness as harness
        batch = {'status': 'paused', 'attempts': []}
        harness.require_engine(batch)
        self.assertEqual(1, batch['review_engine'])
        harness.require_engine({'status': 'ready', 'attempts': [{}]})  # a ready batch can still commit and publish

    def test_invalid_completion_report_is_corrected_without_task_replanning(self):
        from pr_review.common import ApiError
        invoked = 0
        def fix(*args):
            nonlocal invoked
            invoked += 1
            if invoked == 1:
                raise ApiError('invalid_result', 'Missing summary')
            return self.fix(*args)
        with self.engines(fix=fix):
            result = self.f.start(issue_harness=True)
        self.assertEqual(2, invoked)
        self.assertIn('Correct the completion report', self.calls[0][1][-1])
        self.assertEqual('ready', result['batches'][-1]['status'])

    def stop_in_review(self):
        from pr_review.common import ApiError
        def review(*args):
            raise ApiError('model_failed', 'Review interrupted')
        with self.engines(review=review), self.assertRaises(ApiError):
            self.f.start(issue_harness=True)
        batch = self.f.owner.load()['batches'][-1]
        self.assertEqual('review', batch['harness_phase'])
        self.assertEqual([], batch['tasks'])
        self.calls.clear()
        return batch

    def test_interrupted_review_resumes_review_only_by_saved_engine(self):
        batch = self.stop_in_review()
        with self.engines():
            view = self.f.start(batch=batch['id'])
        self.assertEqual(['review'], [r for r, _ in self.calls])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_adopted_source_invalidates_candidate_and_unadopted_source_blocks(self):
        from pr_review.common import ApiError
        batch = self.stop_in_review()
        state = self.f.owner.load()
        (Path(state['worktree']) / 'retained.txt').write_text('External edit')
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaisesRegex(ApiError, 'adopt'):
            self.f.start(batch=batch['id'])
        invoke.assert_not_called()
        with self.engines():
            view = self.f.start(batch=batch['id'], adopt=True)
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertTrue(owner_contracts.current(view['batches'][-1]))

    def test_source_changed_during_review_blocks_acceptance_until_adopted(self):
        from pr_review.common import ApiError
        def review(session, state, batch, specification):
            (Path(state['worktree']) / 'changed-during-review.txt').write_text('edit')
            raise ApiError('integrity_failed', 'The review changed the live owner worktree; inspect the retained changes.')
        with self.engines(review=review), self.assertRaises(ApiError):
            self.f.start(issue_harness=True)
        batch = self.f.owner.load()['batches'][-1]
        self.assertNotEqual('ready', batch['status'])
        self.assertFalse(owner_contracts.current(batch))
        self.assertNotIn('final_tree', batch)
        with patch('pr_review.issue_harness.invoke') as invoke, self.assertRaises(ApiError) as caught:
            self.f.start(batch=batch['id'])
        self.assertEqual('worktree_changed', caught.exception.code)
        invoke.assert_not_called()

    def test_partial_continuation_preserves_findings_for_the_engine_review(self):
        reviews = 0
        def fix(*args):
            result = self.fix(*args)
            if len(self.calls) == 3:
                result['status'] = 'working'
            return result
        def review(*args):
            nonlocal reviews
            reviews += 1
            self.calls.append(('review', None))
            return self.report('changes_requested', ['Original correctness finding']) if reviews == 1 else self.report()
        with self.engines(fix=fix, review=review):
            self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review', 'fix', 'fix', 'review'], [r for r, _ in self.calls])
        self.assertEqual(['Original correctness finding'], self.calls[2][1])
        self.assertIn('Continue the approved', self.calls[3][1][-1])

    def test_ready_candidate_resets_consecutive_no_progress_counter(self):
        fixes = ['working', 'working', 'ready', 'working', 'ready']
        reviews = ['changes_requested', 'ready']
        def fix(*args):
            status = fixes.pop(0)
            result = self.fix(*args)
            return {**result, 'status': status}
        def review(*args):
            self.calls.append(('review', None))
            status = reviews.pop(0)
            return self.report(status, ['Concrete defect'] if status == 'changes_requested' else [])
        with self.engines(fix=fix, review=review):
            view = self.f.start(issue_harness=True)
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(7, len(self.calls))
        self.assertEqual(0, view['batches'][-1]['harness_unchanged'])

    def test_three_identical_rejections_stop(self):
        from pr_review.common import ApiError
        def review(*args):
            self.calls.append(('review', None))
            return self.report('changes_requested', ['Concrete defect'])
        def fix(session, state, batch, specification, feedback):
            self.calls.append(('fix', feedback))
            return self.report()
        with self.engines(fix=fix, review=review), self.assertRaisesRegex(ApiError, 'same unchanged'):
            self.f.start(issue_harness=True)
        self.assertEqual(['fix', 'review'] * 3, [r for r, _ in self.calls])

    def test_review_question_answer_resumes_without_replan(self):
        from pr_review import owner_import
        def review(*args):
            self.calls.append(('review', None))
            return {**self.report('needs_input'), 'questions': ['Keep archived records searchable?']}
        with self.engines(review=review):
            view = self.f.start(issue_harness=True)
        batch = view['batches'][-1]
        self.assertEqual('paused', batch['status'])
        state = self.f.owner.load()
        item = next(i for i in state['feedback'] if i['kind'] == 'issue_plan')
        owner_import.from_issue(self.f.owner, item['source']['plan'],
            {'title': item['title'], 'html_url': item['source']['issue']}, 'workflow',
            {'answers': ['Keep them searchable.']})
        self.calls.clear()
        with self.engines():
            view = self.f.start(batch=batch['id'])
        self.assertEqual(['fix', 'review'], [r for r, _ in self.calls])
        self.assertNotIn('harness_questions', view['batches'][-1])
        self.assertEqual('ready', view['batches'][-1]['status'])

    def test_incomplete_review_writes_no_proof_and_keeps_the_review_phase(self):
        from pr_review import review_engine
        from pr_review.common import ApiError
        value = {'assessment': 'incomplete', 'summary': 'The test environment is broken', 'findings': [],
                 'verification_blockers': [{'command': 'pytest -m smoke', 'reason': 'read-only sysctl',
                                            'manual_command': 'uv run pytest -m smoke -q'}]}
        incomplete = review_engine.Result(value, 'incomplete', Path('/retained/synthesis-incomplete-1.json'))
        with patch('pr_review.issue_harness.invoke', side_effect=self.fix), \
                patch('pr_review.acceptance.review_issue', return_value=(incomplete, None)), \
                self.assertRaises(ApiError) as caught:
            self.f.start(issue_harness=True)
        self.assertEqual('review_incomplete', caught.exception.code)
        self.assertEqual('verify_environment', caught.exception.details['next_action'])
        self.assertEqual(value['verification_blockers'], caught.exception.details['verification_blockers'])
        batch = self.f.owner.load()['batches'][-1]
        self.assertEqual('review', batch['harness_phase'])
        self.assertEqual('paused', batch['status'])
        self.assertFalse(any(k in batch for k in ('harness_review', 'final_tree', 'verified_items')))
        self.assertNotIn('harness_repeated_rejection', batch)
        self.assertFalse(owner_contracts.current(batch))

    def test_findings_return_to_the_same_implementation_session(self):
        from types import SimpleNamespace
        identity = '00000000-0000-4000-8000-000000000001'
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            kwargs['on_line']('stdout', json.dumps({'type': 'thread.started', 'thread_id': identity}))
            (Path(self.f.owner.load()['worktree']) / f'turn-{len(commands)}.txt').write_text('work')
            value = {'status': 'ready', 'summary': 'Tested complete candidate', 'checks': ['ok'], 'questions': [], 'findings': []}
            Path(kwargs['stdout_path']).with_name('result.json').write_text(json.dumps(value))
            return SimpleNamespace(returncode=0, events=[{'type': 'turn.completed'}])
        reviews = 0
        def review(*args):
            nonlocal reviews
            reviews += 1
            return self.report('changes_requested', ['Round trip fails']) if reviews == 1 else self.report()
        state = self.f.owner.load()
        state['settings']['fix']['provider'] = 'codex'
        self.f.owner.save(state)
        with patch('pr_review.issue_harness.invoke', new=REAL_INVOKE), \
                patch('pr_review.issue_harness.run', side_effect=run), \
                patch('pr_review.acceptance.review_issue', side_effect=engine_stub.issue_review_from(review)):
            view = self.f.start(issue_harness=True)
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(2, len(commands))
        self.assertNotIn('resume', commands[0])
        self.assertEqual(['codex', 'exec', 'resume'], commands[1][:3])
        self.assertIn(identity, commands[1])
        self.assertEqual(1, len(self.f.owner.load()['issue_harness_sessions']))


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

    def invoke(self):
        return REAL_INVOKE(self.session, self.state, self.batch, self.spec, [])

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
