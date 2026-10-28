"""Automatic review contracts with real saved Git snapshots and mocked GitHub."""
import copy
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_api
from pr_review import automation, publication
from pr_review.application import dispatch
from pr_review.commands import mutate
from pr_review.common import ApiError, atomic_json, digest, read_json, state_root
from pr_review.state import Session


class AutomationTest(unittest.TestCase):
    def setUp(self):
        self.f = test_api.ApiTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.tearDown)
        self.f.manifest['draft_mutations_tracked'] = True
        atomic_json(self.f.path / 'manifest.json', self.f.manifest)
        self.session = Session({'session': str(self.f.path)})
        self.op = {'operation_id': 'a' * 32, 'log_path': str(state_root() / 'operations' / ('a' * 32 + '.log'))}
        self.run = automation.AutomaticRun(self.op)

    def saved(self, change=None):
        state = self.session.load()
        if change:
            change(state)
        self.session.save(state)
        return state

    def publish(self, **options):
        github, posts = self.f.fake_github(**options)
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            result = self.run.publish(self.session)
        return result, posts

    def generated_duplicates(self, *ids):
        file = self.f.path / 'publication/contributions.json'
        ledger = read_json(file)
        for proposal in ledger['proposals']:
            if proposal['id'] in ids:
                proposal.update(classification='DUPLICATE', body_md='')
                proposal['basis'].update(classification='DUPLICATE', body_md='')
        atomic_json(file, ledger)

    def test_comment_publication_and_repeat_noop(self):
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]['event'], 'COMMENT')
        self.assertTrue(all(c['receipt'] for c in self.session.load()['comments']))
        self.assertTrue(all(c['mutation_actor'] == 'automatic' for c in self.session.load()['comments']))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertFalse(posts)

    def test_each_blocker_stops_all_before_staging(self):
        changes = [dict(classification='UNCERTAIN'), dict(classification='REVISIT'),
                   dict(body=''), dict(placement={'kind': 'unplaced'}),
                   dict(placement={'kind': 'inline', 'path': 'missing', 'side': 'RIGHT', 'line': 2}),
                   dict(partial_coverage=True), dict(needs_attention='Discussion changed')]
        initial = self.session.load()
        for changeset in changes:
            with self.subTest(changes=changeset):
                state = copy.deepcopy(initial)
                state['comments'][1].update(changeset)
                self.session.save(state)
                before = self.session.file.read_bytes()
                result, posts = self.publish()
                self.assertEqual(result['exit_code'], 3)
                self.assertEqual([c['id'] for c in result['affected']], ['p2'])
                self.assertFalse(posts)
                self.assertEqual(before, self.session.file.read_bytes())

    def test_outdated_and_resolved_replies_need_judgment(self):
        for key in ('is_outdated', 'is_resolved'):
            state = self.session.load()
            state['discussion']['items'] = [dict(key='thread:T', kind='thread', root_id=9, can_reply=True,
                                                **{key: True})]
            state['comments'][0]['placement'] = dict(kind='reply', key='thread:T', root_id=9)
            self.session.save(state)
            result, posts = self.publish()
            self.assertEqual(result['exit_code'], 3)
            self.assertFalse(posts)

    def test_automatic_duplicates_and_empty_findings_are_noops(self):
        self.generated_duplicates('p1', 'p2')
        comments = self.session.load()['comments']
        self.assertEqual([c['body'] for c in comments], ['Comment 1', 'Comment 2'])
        self.assertTrue(all(c['status'] == 'suppressed' and not c['human_edited'] for c in comments))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(posts)
        self.saved(lambda s: s.update(comments=[]))
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'noop')
        self.assertFalse(posts)

    def test_generated_duplicate_does_not_block_mixed_publication(self):
        self.generated_duplicates('p1')
        result, posts = self.publish()
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0][1]['comments']), 1)
        self.assertEqual(posts[0][1]['comments'][0]['body'].split('\n\n<!--')[0], 'Comment 2')
        duplicate, published = self.session.load()['comments']
        self.assertEqual(duplicate['status'], 'suppressed')
        self.assertFalse(duplicate['human_edited'])
        self.assertEqual(published['status'], 'published')

    def test_generated_duplicate_does_not_block_replacement_before_publication(self):
        self.generated_duplicates('p1')
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        self.assertIsNone(self.run.before_analysis(self.session.root))
        self.assertIsNone(automation.guard_replacement(self.session))
        self.assertFalse(self.session.file.exists())

    def test_edited_duplicate_still_requires_manual_completion(self):
        self.generated_duplicates('p1')
        file = self.f.path / 'publication/contributions.json'
        ledger = read_json(file)
        ledger['proposals'][0]['body_md'] = 'Human duplicate wording'
        atomic_json(file, ledger)
        state = self.saved()
        self.assertTrue(state['comments'][0]['human_edited'])
        self.assertEqual(self.session.load()['comments'][0]['body'], 'Human duplicate wording')
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            automation.guard_replacement(self.session)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('manual decisions', result['reason'])
        self.assertFalse(posts)

    def test_manual_mutations_and_verdict_prevent_noop_or_publication(self):
        initial = self.session.load()
        for request in ({'operation': 'edit', 'comment': 'p1', 'body': 'Human wording'},
                        {'operation': 'stage', 'comment': 'p1'},
                        {'operation': 'suppress', 'comment': 'p1'},
                        {'operation': 'acknowledge', 'comment': 'p1'},
                        {'operation': 'placement', 'comment': 'p1', 'placement': {'kind': 'review'}},
                        {'operation': 'verdict', 'verdict': 'COMMENT'}):
            with self.subTest(request=request):
                state = copy.deepcopy(initial)
                mutate(self.session, state, {**request, 'actor': 'automatic'})
                result, posts = self.publish()
                self.assertEqual(result['exit_code'], 3)
                self.assertFalse(posts)
        state = copy.deepcopy(initial)
        state.update(comments=[], verdict_requested=True, verdict='APPROVE')
        self.session.save(state)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(posts)

    def test_legacy_saved_drafts_require_manual_completion(self):
        self.saved(lambda s: s.pop('mutation_tracking'))
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('unknown', result['reason'])
        self.assertFalse(posts)

    def test_read_only_inspection_leaves_drafts_and_run_record_unchanged(self):
        self.saved(lambda s: s['comments'][0].update(classification='UNCERTAIN'))
        self.publish()
        files = [self.session.file, *self.session.root.joinpath('automatic-runs').glob('*.json')]
        before = {p: p.read_bytes() for p in files}
        first = dispatch({'command': 'read', 'session': str(self.session.root)})
        second = dispatch({'command': 'read', 'session': str(self.session.root)})
        self.assertEqual(first, second)
        self.assertEqual(first['analysis']['displayed']['status'], 'complete')
        self.assertEqual(first['automation']['outcome'], 'needs_attention')
        self.assertEqual(before, {p: p.read_bytes() for p in files})

    def test_lost_response_is_read_back_without_resending(self):
        result, posts = self.publish(ambiguous_review=True)
        self.assertEqual(result['outcome'], 'published')
        self.assertEqual(len(posts), 1)

    def test_unsent_remainder_survives_repeated_automatic_attempt(self):
        github, posts = self.f.fake_github()
        def unavailable(session, endpoint, *args, **kwargs):
            if args:
                raise ApiError('network', 'No response')
            return github(session, endpoint, *args, **kwargs)
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=unavailable):
            first = self.run.publish(self.session)
            second = self.run.before_analysis(self.session.root)
        self.assertEqual(first['exit_code'], 3)
        self.assertEqual(second['exit_code'], 3)
        self.assertFalse(posts)
        self.assertEqual(automation.actions(self.session.load())[0]['status'], 'failed')

    def test_uncertainty_takes_precedence_when_receipt_lookup_fails(self):
        def unavailable(*args, **kwargs):
            raise ApiError('network', 'Unavailable')
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=unavailable):
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 5)

    def test_mid_batch_draft_remainder_is_not_archived(self):
        state = self.saved()
        state['batches'] = [{'actions': [{'status': 'published', 'result': {'html_url': 'https://test/receipt'}},
                                       {'status': 'draft'}]}]
        self.session.save(state)
        with patch.object(publication, 'gh', side_effect=AssertionError('No lookup needed')):
            result = self.run.before_analysis(self.session.root)
        self.assertEqual(result['exit_code'], 4)
        self.assertEqual(automation.actions(self.session.load())[1]['status'], 'draft')

    def test_prepare_change_never_publishes_reduced_selection(self):
        def prepare(session, state):
            state['comments'][0]['status'] = 'unstaged'
            session.save(state)
            return {'ready': True, 'confirmation': 'token'}
        with patch.object(publication, 'prepare', side_effect=prepare), patch.object(publication, 'publish') as publish:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        publish.assert_not_called()

    def test_paused_record_does_not_create_empty_draft_ledger(self):
        (self.f.path / 'publication/contributions.json').unlink()
        self.f.manifest['status'] = 'paused'
        atomic_json(self.f.path / 'manifest.json', self.f.manifest)
        session = Session({'session': str(self.f.path)})
        result = self.run.publish(session)
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(session.file.exists())
        atomic_json(self.f.path / 'publication/contributions.json', {'proposals': [
            {'id': 'p1', 'basis': {'finding_ids': ['F1'], 'body_md': 'Retained finding'},
             'body_md': 'Retained finding', 'classification': 'NEW'}]})
        self.assertEqual(session.load()['comments'][0]['body'], 'Retained finding')

    def test_stale_generation_and_manual_replacement_guard(self):
        self.saved()
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        state = self.session.load()
        mutate(self.session, state, {'operation': 'edit', 'comment': 'p1', 'body': 'Do not replace'})
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            automation.guard_replacement(self.session)

    def test_saved_log_is_session_scoped(self):
        self.run.bind(self.session)
        path = state_root() / 'operations' / ('a' * 32 + '.log')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Saved model progress\n')
        result = dispatch({'command': 'review-log', 'session': str(self.session.root), 'operation_id': 'a' * 32})
        self.assertEqual(result['text'], path.read_text())
        for identity in ('../secret', 'b' * 32):
            with self.assertRaises(ApiError):
                dispatch({'command': 'review-log', 'session': str(self.session.root), 'operation_id': identity})

    def test_interrupted_record_is_reported_without_rewriting_it(self):
        self.run.bind(self.session)
        path = next((self.session.root / 'automatic-runs').glob('*.json'))
        before = path.read_bytes()
        self.assertEqual(automation.summary(self.session)['outcome'], 'interrupted')
        self.assertEqual(before, path.read_bytes())

    def test_system_refresh_is_not_manual_intervention(self):
        state = self.saved()
        with patch.object(publication, 'repository_operation'):
            publication.refresh(self.session, state)
        self.assertIsNone(automation.manual_reason(state))

    def test_promotion_guard_preserves_manual_edits_made_during_analysis(self):
        from pr_review.sessions import Store
        from pr_review.storage import hash_file
        self.saved()
        candidate = Path(str(self.session.root) + '-history') / 'attempt-guard'
        shutil.copytree(self.session.root, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest.update(session_directory=str(self.session.root), attempt={'status': 'building'},
                        phases={'synthesis': {'status': 'completed', 'output_hash': hash_file(candidate / 'reviews/synthesis.json')}})
        atomic_json(candidate / 'manifest.json', manifest)
        (self.session.root / '.pending-review').symlink_to(candidate)
        store = Store.__new__(Store)
        store.root, store.path, store.manifest = self.session.root, candidate, manifest
        token = automation._active.set(self.run)
        self.addCleanup(automation._active.reset, token)
        state = self.session.load()
        mutate(self.session, state, {'operation': 'edit', 'comment': 'p1', 'body': 'Edit during analysis'})
        with self.assertRaisesRegex(ApiError, 'manual decisions'):
            store.promote(['synthesis'])
        self.assertFalse((self.session.root / '.current-review').exists())
        self.assertTrue((candidate / 'reviews/synthesis.json').exists())
        self.assertEqual(self.session.load()['comments'][0]['body'], 'Edit during analysis')

    def test_saved_trust_token_and_first_run_association(self):
        from types import SimpleNamespace
        from pr_review import models, reviews, settings
        from pr_review.repository import Repository
        repo = SimpleNamespace(common=self.f.repo / '.git', pr=7, lock=self.session.pr_lock)
        def fail(request):
            automation.observe(self.session)
            raise ApiError('confirmation_required', 'Trust required', snapshot='head:base', paths=['AGENTS.md'])
        with patch.object(Repository, 'resolve', return_value=repo), \
                patch('pr_review.sessions.locate', return_value=(None, self.f.repo)), \
                patch.object(settings, 'resolve', return_value={'settings': settings.DEFAULTS}), \
                patch.object(models, 'discover'), patch.object(models, 'validate_selection'), \
                patch.object(reviews, 'execute', side_effect=fail):
            result = automation.run({'pr': 7})
        self.assertEqual(result['exit_code'], 3)
        self.assertEqual(result['trusted_snapshot'], 'head:base')
        self.assertEqual(result['session'], str(self.session.root))
        self.assertEqual(result['agent_config_paths'], ['AGENTS.md'])
        self.assertTrue(Path(result['log_path']).exists())

    def test_noop_still_checks_freshness(self):
        self.saved(lambda s: s.update(comments=[]))
        def validate(session, operation, verdict='COMMENT'):
            if operation == 'validate':
                raise ApiError('operation_failed', 'PR head changed')
        with patch.object(publication, 'repository_operation', side_effect=validate), patch.object(publication, 'gh') as gh:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        gh.assert_not_called()

    def test_noop_does_not_ignore_discussion_changes_without_staged_drafts(self):
        self.saved(lambda s: s.update(comments=[]))
        def refresh(session, state):
            state['discussion_hash'] = 'changed-after-analysis'
            return []
        with patch.object(publication, 'refresh', side_effect=refresh):
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)

    def test_final_publication_check_does_not_resend_a_late_failed_batch(self):
        state = self.saved()
        state['batches'] = [{'actions': [{'status': 'draft'}]}]
        self.session.save(state)
        with patch.object(publication, 'publish') as publish:
            result = self.run.publish(self.session)
        self.assertEqual(result['exit_code'], 3)
        publish.assert_not_called()

    def test_newer_pending_analysis_blocks_publication_of_previous_completed_result(self):
        candidate = Path(str(self.session.root) + '-history') / 'attempt-pending'
        shutil.copytree(self.session.root, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest.update(session_directory=str(self.session.root), status='paused')
        atomic_json(candidate / 'manifest.json', manifest)
        (self.session.root / '.pending-review').symlink_to(candidate)
        result, posts = self.publish()
        self.assertEqual(result['exit_code'], 3)
        self.assertFalse(posts)
