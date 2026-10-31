"""Fingerprint-bound completion without commits, using independent Git fixtures."""
import copy
from pathlib import Path
import unittest

from pr_review.owner_state import OwnerSession
import test_owner


class NoncommittingTest(unittest.TestCase):
    git = test_owner.OwnerTest.git
    github = test_owner.OwnerTest.github
    publish = test_owner.OwnerTest.publish
    request = test_owner.OwnerTest.request
    item = staticmethod(test_owner.OwnerTest.item)
    saved_review = test_owner.OwnerTest.saved_review
    handoff = test_owner.OwnerTest.handoff

    def setUp(self):
        test_owner.OwnerTest.setUp(self)
        self.review = self.saved_review()
        self.keys = self.handoff(self.review, ['p1', 'p2'])['items']

    def model(self, session, state, batch, role, context):
        if role == 'plan':
            return test_owner.OwnerTest.model(self, session, state, batch, role, context)
        self.calls.append((role, context))
        return {'summary': 'No changes needed', 'outcomes': [
            {'item': i['key'], 'status': 'already_addressed' if n == 0 else 'no_change',
             'evidence': 'Traced the current behavior'} for n, i in enumerate(context['feedback'])],
            'complete': True, 'checks': ['source tracing'], 'blocking': []}

    def finish(self):
        return self.request('owner-fix', items=self.keys, auto_commit=True)

    def test_verified_outcomes_survive_automatic_import_but_explicit_requeue_wins(self):
        view = self.finish()
        self.assertFalse(view['batches'][-1]['commit_plan']['units'])
        for item in view['feedback']:
            if item['key'] in self.keys:
                self.assertFalse(item['attention'])
                self.assertEqual(item['fingerprint'], item['local_completion']['fingerprint'])
        calls = len(self.calls)
        for _ in range(2):
            self.request('owner-refresh')
            self.assertEqual([], self.handoff(self.review, ['p1', 'p2'], automatic=True)['items'])
        # An unrelated draft edit changes the revision, but not p1's fingerprint.
        data = self.review.load()
        data['comments'][1]['body'] += ' More detail.'
        self.review.save(data)
        self.assertEqual([], self.handoff(self.review, ['p1'], automatic=True)['items'])
        self.assertEqual(calls, len(self.calls))
        self.assertFalse(self.posts)
        self.assertFalse(view['replies'])
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD', cwd=view['worktree']))
        self.assertEqual([self.keys[0]], self.handoff(self.review, ['p1'])['items'])
        view = self.request('owner-reconcile')
        self.assertTrue(next(i for i in view['feedback'] if i['key'] == self.keys[0])['attention'])
        self.assertEqual([self.keys[0]], self.handoff(self.review, ['p1'], automatic=True)['items'])
        self.request('owner-mutate', operation='restore', items=[self.keys[1]])
        self.assertEqual([self.keys[1]], self.handoff(self.review, ['p2'], automatic=True)['items'])

    def test_changed_finding_and_review_snapshot_are_reimported(self):
        self.finish()
        data = self.review.load()
        data['comments'][0]['body'] += ' New requirement.'
        self.review.save(data)
        changed = self.handoff(self.review, ['p1'], automatic=True)
        self.assertEqual([self.keys[0]], changed['items'])
        self.assertTrue(changed['owner']['batches'][-1]['discussion_stale'])
        self.assertTrue(changed['owner']['feedback'][0]['attention'])
        # Changing only the saved review's base also changes its fingerprint.
        from pr_review.common import atomic_json
        manifest = copy.deepcopy(self.review.manifest)
        manifest['snapshot']['base'] = self.head
        atomic_json(self.review.root / 'manifest.json', manifest)
        self.assertEqual([self.keys[1]], self.handoff(self.review, ['p2'], automatic=True)['items'])

    def test_invalidated_verification_reopens_noncommitting_findings(self):
        self.finish()
        session = OwnerSession({'session': self.path})
        original = session.load()
        changes = [lambda b: b.update(status='paused'),
                   lambda b: b.update(integrity_failure='changed'),
                   lambda b: b.update(discussion_stale=True),
                   lambda b: b.update(verified_items=[]),
                   lambda b: b['verified_fingerprints'].update({self.keys[0]: 'old'}),
                   lambda b: b['review'].update(complete=False),
                   lambda b: b['review'].update(blocking=['unverified']),
                   lambda b: b['commit_execution'].update(status='paused'),
                   lambda b: b['commit_execution'].update(inspection_required=True),
                   lambda b: b.update(verified_base='different-base')]
        for change in changes:
            with self.subTest(change=change):
                state = copy.deepcopy(original)
                state['sequence'] = session.load()['sequence']
                change(state['batches'][-1])
                session.save(state)
                view = self.request('owner-reconcile')
                self.assertTrue(next(i for i in view['feedback'] if i['key'] == self.keys[0])['attention'])
                self.assertEqual([self.keys[0]], self.handoff(self.review, ['p1'], automatic=True)['items'])
        state = copy.deepcopy(original)
        state['sequence'] = session.load()['sequence']
        state['metadata']['baseRefOid'] = 'new-base'
        session.save(state)
        self.assertEqual([self.keys[0]], self.handoff(self.review, ['p1'], automatic=True)['items'])

    def test_source_index_and_head_changes_invalidate_completion(self):
        view = self.finish()
        session = OwnerSession({'session': self.path})
        original = session.load()
        path = Path(view['worktree']) / 'unverified.txt'
        path.write_text('unverified\n')
        self.assertEqual(self.keys, self.handoff(self.review, ['p1', 'p2'], automatic=True)['items'])
        self.git('add', 'unverified.txt', cwd=view['worktree'])
        session.save({**copy.deepcopy(original), 'sequence': session.load()['sequence']})
        self.assertEqual(self.keys, self.handoff(self.review, ['p1', 'p2'], automatic=True)['items'])
        self.git('commit', '-qm', 'Changed source', cwd=view['worktree'])
        session.save({**copy.deepcopy(original), 'sequence': session.load()['sequence']})
        self.assertEqual(self.keys, self.handoff(self.review, ['p1', 'p2'], automatic=True)['items'])
