"""Evaluation regressions against the current owner workflow, with real Git."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_owner as fixtures
from pr_review import owner_git, owner_fixes
from pr_review.api import dispatch
from pr_review.common import ApiError
from pr_review.owner_state import OwnerSession
from pr_review.processes import run as process_run


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.t = fixtures.OwnerTest()
        self.addCleanup(self.t.doCleanups)
        self.t.setUp()

    def split(self, *args):
        if args[3] == 'plan':
            return {'tasks': [dict(id='t' + str(i), items=[item['key']], depends_on=[],
                                   rationale='Independent fix', expected=[dict(item=item['key'], outcome='fixed')])
                              for i, item in enumerate(args[4]['feedback'])]}
        return self.t.model(*args)

    def interrupted(self):
        def model(*args):
            value = self.split(*args)
            if args[3] == 'fix':
                raise ApiError('cancelled', 'Stopped after partial edits')
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with self.assertRaises(ApiError):
                self.t.fix()
        return self.t.request('owner-read')['batches'][-1]

    def test_unrelated_skip_cannot_restart_interrupted_task(self):
        batch = self.interrupted()
        self.t.calls.clear()
        with self.assertRaises(ApiError) as caught:
            self.t.request('owner-fix', batch=batch['id'], skip=[batch['tasks'][1]['id']])
        self.assertEqual(caught.exception.code, 'interrupted_task')
        self.assertFalse(self.t.calls)
        # Explicitly skipping the interrupted task still permits independent work.
        self.t.request('owner-fix', batch=batch['id'], skip=[batch['tasks'][0]['id']])
        self.assertTrue(any(role == 'fix' for role, _ in self.t.calls))

    def test_adoption_retains_queued_tasks_initial_fixer(self):
        batch = self.interrupted()
        self.t.calls.clear()
        self.t.request('owner-fix', batch=batch['id'], adopt=True)
        fixes = [c for role, c in self.t.calls if role == 'fix']
        self.assertEqual([[i['key'] for i in c['feedback']] for c in fixes], [['thread:T2']])

    def test_failed_preflight_preserves_ready_fix_for_commit_association(self):
        batch = self.t.fix()['batches'][-1]
        with patch('pr_review.owner_providers.preflight', side_effect=ApiError('authentication_failed', 'Offline')):
            with self.assertRaises(ApiError):
                self.t.request('owner-fix', batch=batch['id'])
        current = self.t.request('owner-read')['batches'][-1]
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(current['final_tree'], batch['final_tree'])
        self.t.commit_fix()
        self.assertEqual(len(self.t.request('owner-associate')['replies']), 2)

    def test_changed_feedback_does_not_preserve_ready_verification(self):
        batch = self.t.fix()['batches'][-1]
        self.t.feedback[0]['comments'][0]['body'] = 'New concern'
        with self.assertRaises(ApiError):
            self.t.request('owner-fix', batch=batch['id'])
        self.assertNotEqual(self.t.request('owner-read')['batches'][-1]['status'], 'ready')

    def test_skips_survive_replanning_and_deleted_items_can_be_excluded(self):
        with patch('pr_review.owner_providers.invoke', side_effect=self.split):
            batch = self.t.fix()['batches'][-1]
            self.t.feedback.pop(0)
            result = self.t.request('owner-fix', batch=batch['id'], replan=True, skip=[batch['tasks'][0]['id']])
            current = result['batches'][-1]
            self.assertEqual(current['status'], 'ready')
            self.assertEqual(current['verified_items'], ['thread:T2'])
            self.assertEqual(current['tasks'][0]['status'], 'skipped')
            self.assertFalse(self.t.request('owner-refresh')['batches'][-1]['discussion_stale'])
            self.t.calls.clear()
            self.t.request('owner-fix', batch=batch['id'], replan=True)
            self.assertFalse(any(i['key'] == 'thread:T1' for _, c in self.t.calls for i in c['feedback']))

    def test_replanning_preserves_skips_even_when_feedback_still_exists(self):
        with patch('pr_review.owner_providers.invoke', side_effect=self.split):
            batch = self.t.fix()['batches'][-1]
            self.t.calls.clear()
            current = self.t.request('owner-fix', batch=batch['id'], replan=True,
                                     skip=[batch['tasks'][0]['id']])['batches'][-1]
        self.assertEqual(current['tasks'][0]['status'], 'skipped')
        self.assertEqual(current['verified_items'], ['thread:T2'])
        self.assertFalse(any(i['key'] == 'thread:T1' for _, c in self.t.calls for i in c['feedback']))

    def test_replan_then_adopt_has_no_dangling_completed_dependencies(self):
        def model(*args):
            value = self.split(*args)
            if args[3] == 'plan' and len(value['tasks']) == 2:
                value['tasks'][1]['depends_on'] = ['t0']
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix()['batches'][-1]
            self.t.feedback[0]['comments'][0]['body'] = 'Changed first concern'
            self.t.request('owner-fix', batch=batch['id'], replan=True)
            current = self.t.request('owner-fix', batch=batch['id'], adopt=True)['batches'][-1]
        self.assertEqual(current['status'], 'ready')
        ids = {t['id'] for t in current['tasks']}
        self.assertTrue(all(set(t['depends_on']) <= ids for t in current['tasks']))

    def test_legacy_clarification_does_not_launch_task_or_combined_repairs(self):
        for combined in (False, True):
            with self.subTest(combined=combined):
                # Each case has an independent clean worktree and batch.
                if combined:
                    self.t.doCleanups()
                    self.t.setUp()
                def model(*args):
                    value = self.t.model(*args)
                    if args[3] == 'review' and bool(args[4].get('scope')) == combined:
                        value['blocking'] = ['Need an owner decision']
                        value['outcomes'][0]['status'] = 'clarify'
                    return value
                with patch('pr_review.owner_providers.invoke', side_effect=model):
                    view = self.t.fix()
                self.assertEqual(view['batches'][-1]['status'], 'paused')
                self.assertEqual(sum(role == 'fix' for role, _ in self.t.calls), 1)

    def test_failed_combined_repair_requires_adoption_with_actual_provider_wrapper(self):
        combined_reviews = []
        def fail_process(_argv, **kwargs):
            code = "from pathlib import Path; Path('partial.txt').write_text('retained'); raise SystemExit(1)"
            return process_run([sys.executable, '-c', code], **{**kwargs, 'timeout': 5})
        def model(*args):
            if args[3] == 'fix' and args[4].get('scope'):
                with patch('pr_review.owner_providers.run', side_effect=fail_process):
                    return fixtures.REAL_INVOKE(*args)
            value = self.t.model(*args)
            if args[3] == 'review' and args[4].get('scope'):
                combined_reviews.append(args[4])
                if len(combined_reviews) == 1:
                    value['blocking'] = ['Fix interaction']
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            with self.assertRaises(ApiError):
                self.t.fix()
            batch = self.t.request('owner-read')['batches'][-1]
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-fix', batch=batch['id'])
            self.assertEqual(caught.exception.code, 'interrupted_task')
            self.assertEqual(len(combined_reviews), 1)
            current = self.t.request('owner-fix', batch=batch['id'], adopt=True)['batches'][-1]
            self.assertEqual(current['status'], 'ready')
            self.assertIn('partial.txt', combined_reviews[-1]['batch_diff'])
            self.assertEqual(self.t.request('owner-fix', batch=batch['id'])['batches'][-1]['status'], 'ready')

    def test_uncertain_reply_keeps_submitted_payload_during_reassociation(self):
        batch = self.t.fix()['batches'][-1]
        sha = self.t.commit_fix()
        view = self.t.request('owner-associate')
        original = dict(view['replies'][0])
        preview = self.t.request('owner-prepare')
        def uncertain(*args, **kwargs):
            self.t.publish(*args, **kwargs)
            raise ApiError('offline', 'Lost reply response')
        with patch('pr_review.owner_publication.gh', side_effect=uncertain), self.assertRaises(ApiError):
            self.t.request('owner-publish', token=preview['token'])
        self.t.git('commit', '--amend', '-m', 'Amended fix', cwd=view['worktree'])
        amended = self.t.git('rev-parse', 'HEAD', cwd=view['worktree'])
        self.assertNotEqual(sha, amended)
        self.t.git('update-ref', 'refs/pull/7/head', amended)
        self.t.metadata['headRefOid'] = amended
        view = self.t.request('owner-associate', batch=batch['id'], commits=[amended])
        reply = next(r for r in view['replies'] if r['id'] == original['id'])
        self.assertEqual(reply['body'], original['body'])
        self.assertEqual(reply['commits'], [sha])
        view = self.t.request('owner-recover')
        reply = next(r for r in view['replies'] if r['id'] == original['id'])
        self.assertEqual(reply['status'], 'published')
        self.assertIn(sha, reply['receipt']['body'])

    def test_pr_closure_between_replies_preserves_receipts_and_stops_sending(self):
        for item in self.t.feedback:
            self.t.request('owner-mutate', operation='reply', item=item['key'], body='Reply')
        ids = [r['id'] for r in self.t.view['replies']]
        self.t.request('owner-mutate', operation='stage', replies=ids)
        preview = self.t.request('owner-prepare')
        def publish(*args, **kwargs):
            receipt = self.t.publish(*args, **kwargs)
            self.t.metadata['state'] = 'CLOSED'
            return receipt
        with patch('pr_review.owner_publication.gh', side_effect=publish):
            with self.assertRaises(ApiError) as caught:
                self.t.request('owner-publish', token=preview['token'])
        self.assertEqual(caught.exception.code, 'closed_pr')
        self.assertEqual(len(self.t.posts), 1)
        self.assertEqual(sum(r['status'] == 'published' for r in self.t.request('owner-read')['replies']), 1)

    def test_missing_association_does_not_abort_surviving_reply(self):
        batch = self.t.fix()['batches'][-1]
        sha = self.t.commit_fix()
        self.t.request('owner-associate')
        self.t.feedback.pop(0)
        view = self.t.request('owner-associate', batch=batch['id'], commits=[sha], items=['thread:T2'])
        self.assertTrue(any(r['source'] == 'thread:T2' for r in view['replies']))
        self.assertTrue(any(i['item'] == 'thread:T1' for i in view['batches'][-1]['reply_issues']))

    def test_restored_acknowledged_general_comment_returns_to_attention(self):
        self.t.feedback.append(self.t.item('comment:3', 3, kind='comment'))
        self.t.request('owner-refresh')
        self.t.request('owner-mutate', operation='acknowledge', items=['comment:3'])
        view = self.t.request('owner-mutate', operation='restore', items=['comment:3'])
        item = next(i for i in view['feedback'] if i['key'] == 'comment:3')
        self.assertTrue(item['attention'])
        self.assertTrue(item['queued'])

    def test_confirmed_receipt_does_not_interrupt_two_replies_to_same_thread(self):
        self.t.fix()
        for body in ('First reply', 'Second reply'):
            self.t.request('owner-mutate', operation='reply', item='thread:T1', body=body)
        self.t.request('owner-mutate', operation='stage', replies=[r['id'] for r in self.t.view['replies']])
        preview = self.t.request('owner-prepare')
        def publish(*args, **kwargs):
            receipt = self.t.publish(*args, **kwargs)
            comment = copy.deepcopy(self.t.feedback[0]['comments'][0])
            comment.update(id=receipt['id'], author='author', body=receipt['body'])
            self.t.feedback[0]['comments'].append(comment)
            return receipt
        with patch('pr_review.owner_publication.gh', side_effect=publish):
            self.t.request('owner-publish', token=preview['token'])
        self.assertEqual(len(self.t.posts), 2)
        self.assertFalse(self.t.request('owner-refresh')['batches'][-1]['discussion_stale'])

    def test_ambiguous_toolkit_selection_requires_repository_before_saving(self):
        self.t.git('remote', 'add', 'upstream', 'https://github.com/parent/repo.git')
        request = dict(protocol=1, command='toolkit', cwd=str(self.t.repo), toolkit='owner')
        with self.assertRaises(ApiError) as caught:
            dispatch(request)
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertEqual(dispatch({**request, 'repository': 'github.com/parent/repo'})['toolkit'], 'owner')

    def test_context_omits_identical_diff_and_bounds_ignored_inventory(self):
        def model(*args):
            value = self.t.model(*args)
            if args[3] == 'fix':
                session, state, batch = args[:3]
                root = Path(state['worktree'])
                (root / '.gitignore').write_text('generated/\n')
                (root / 'generated').mkdir()
                for i in range(60):
                    (root / 'generated' / str(i)).write_text('ignored')
                batch['checkpoint'] = owner_git.checkpoint(session, state, 'ignored fixtures')
            return value
        with patch('pr_review.owner_providers.invoke', side_effect=model):
            batch = self.t.fix()['batches'][-1]
        self.assertEqual(batch['checkpoint']['ignored_count'], 60)
        self.assertLessEqual(len(batch['checkpoint']['ignored']), 20)
        final = next(c for role, c in self.t.calls if role == 'review' and c.get('scope'))
        self.assertNotIn('task_diff', final)
        session = OwnerSession({'session': self.t.path})
        data = owner_fixes.context(session, session.load(), batch, batch['items'], batch['checkpoint']['tree'])
        self.assertEqual(data['task_diff'], '')  # Distinct empty task diff must remain.

    def test_checkpoint_refuses_dirty_submodule_contents(self):
        sub = self.t.root / 'module'
        sub.mkdir()
        self.t.git('init', '-q', '-b', 'main', cwd=sub)
        (sub / 'nested.txt').write_text('original')
        self.t.git('add', '.', cwd=sub)
        self.t.git('commit', '-qm', 'module', cwd=sub)
        self.t.git('-c', 'protocol.file.allow=always', 'submodule', 'add', str(sub), 'vendor')
        self.t.git('commit', '-qam', 'Add module')
        head = self.t.git('rev-parse', 'HEAD')
        self.t.git('update-ref', 'refs/pull/7/head', head)
        self.t.metadata['headRefOid'] = head
        self.t.request('owner-refresh')
        session = OwnerSession({'session': self.t.path})
        state = session.load()
        owner_git.prepare(session, state)
        worktree = Path(state['worktree'])
        self.t.git('-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', cwd=worktree)
        owner_git.checkpoint(session, state, 'clean module')
        (worktree / 'vendor/nested.txt').write_text('unsnapshotted change')
        with self.assertRaises(ApiError) as caught:
            owner_git.checkpoint(session, state, 'dirty module')
        self.assertEqual(caught.exception.code, 'dirty_submodule')
        self.assertEqual((worktree / 'vendor/nested.txt').read_text(), 'unsnapshotted change')
