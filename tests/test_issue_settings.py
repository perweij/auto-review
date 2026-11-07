"""Settings transitions through real issue/Owner state with scripted providers."""
import copy
from contextlib import redirect_stderr
from io import StringIO
import unittest
from unittest.mock import patch

import test_issues
import test_issue_engine
from pr_review import issue_settings, owner_state
from pr_review.common import ApiError, digest
from pr_review.issues import Workflow, dispatch


class ResumeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issues.IssueIntegrationTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.output = StringIO()
        sink = redirect_stderr(self.output)
        sink.__enter__()
        self.addCleanup(sink.__exit__, None, None, None)

    def request(self, *args):
        return self.t.terminal_request(*args)

    def state(self):
        return dispatch({**self.t.without_flags(), 'command': 'issue-read'})

    def pause(self):
        self.assertEqual('approval', dispatch(self.request('--max-runtime', '2h'))['outcome'])
        return self.t.interrupt_recorded_implementation(self.request('--approve', '1'))

    def test_model_effort_change_keeps_approval_batch_and_consumption(self):
        before = self.pause()
        old = self.state()
        fix_name = old['configuration']['_named']['fix']
        result = dispatch(self.request('--' + fix_name + '-model', 'gpt-6.1-terra',
                                       '--' + fix_name + '-effort', 'medium', '--max-runtime', '3h'))
        self.assertEqual('ready', result['outcome'], result)
        after = self.state()
        data = owner_state.OwnerSession({'session': self.t.path}).load()
        self.assertEqual(1, len(self.t.assessments))
        self.assertEqual(old['plan'], after['plan'])
        self.assertEqual(old['approval']['digest'], after['approval']['digest'])
        self.assertEqual(before['batches'][0]['id'], data['batches'][0]['id'])
        self.assertEqual('gpt-6.1-terra', data['batches'][0]['settings']['fix']['model'])
        self.assertEqual(before['batches'][0]['attempts'][0], data['batches'][0]['attempts'][0])
        self.assertGreaterEqual(after['resources']['active_seconds'], old['resources']['active_seconds'])
        self.assertEqual(10800, after['resources']['max_runtime'])
        self.assertEqual('applied', after['settings_transitions'][-1]['status'])
        self.assertEqual(1, len(data['batches'][0]['settings_transitions']))
        self.assertEqual(old['configuration'], after['assessment_configuration'])

    def test_provider_change_updates_effective_execution_and_quota_provider_scope(self):
        self.pause()
        result = dispatch(self.request('--fix', 'claude-7', '--claude-7-model', 'sonnet'))
        self.assertEqual('ready', result['outcome'], result)
        data = owner_state.OwnerSession({'session': self.t.path}).load()
        self.assertEqual('claude', data['batches'][0]['settings']['fix']['provider'])
        self.assertIn('claude', self.state()['resources']['providers'])
        self.assertEqual(1, len(self.t.assessments))

    def test_planning_model_change_keeps_the_completed_approved_assessment(self):
        self.pause()
        old = self.state()
        name = old['configuration']['_named']['planning']
        result = dispatch(self.request('--' + name + '-effort', 'medium'))
        self.assertEqual('ready', result['outcome'], result)
        self.assertEqual(1, len(self.t.assessments))
        self.assertEqual(old['assessment_configuration'], self.state()['assessment_configuration'])

    def test_model_change_does_not_hide_new_requirements(self):
        self.pause()
        name = self.state()['configuration']['_named']['fix']
        self.t.f['issue']['body'] += ' Also retain history.'
        self.assertEqual('approval', dispatch(self.request('--' + name + '-effort', 'medium'))['outcome'])
        self.assertEqual(2, len(self.t.assessments))
        self.assertEqual(2, self.state()['plan']['revision'])
        self.assertNotIn('approval', self.state())

    def test_restart_after_owner_transition_save_does_not_duplicate_transition(self):
        self.pause()
        name = self.state()['configuration']['_named']['fix']
        real = issue_settings.change_batch
        def saved_then_stopped(*args):
            real(*args)
            raise ApiError('model_failed', 'Stopped after saving the new batch choices')
        with patch('pr_review.issue_settings.change_batch', side_effect=saved_then_stopped):
            result = dispatch(self.request('--' + name + '-effort', 'medium'))
        self.assertEqual('model_failed', result['error']['code'])
        self.assertEqual('ready', dispatch(self.request())['outcome'])
        data = owner_state.OwnerSession({'session': self.t.path}).load()
        self.assertEqual(1, len(data['batches'][0]['settings_transitions']))
        self.assertEqual(1, len(self.state()['settings_transitions']))
        self.assertEqual(1, len(self.t.assessments))

    def test_runtime_limit_resume_changes_model_without_another_planner_or_approval(self):
        self.pause()
        flow = Workflow(self.request(), self.t.repo_object, self.t.f)
        flow.state['resources']['active_seconds'] = 7200
        flow.save()
        limited = dispatch(self.request())
        self.assertEqual('increase_runtime', limited['next_action'], limited)
        name = self.state()['configuration']['_named']['fix']
        result = dispatch(self.request('--' + name + '-model', 'gpt-6.1-terra', '--max-runtime', '3h'))
        self.assertEqual('ready', result['outcome'], result)
        self.assertEqual(1, len(self.t.assessments))
        self.assertEqual(1, self.state()['plan']['revision'])
        self.assertGreaterEqual(self.state()['resources']['active_seconds'], 7200)

    def test_invalid_regeneration_request_does_not_poison_normal_resume(self):
        self.pause()  # No complete implementation candidate exists yet.
        result = dispatch(self.request('--rerun-review', 'synthesis'))
        self.assertEqual('invalid_request', result['error']['code'], result)
        self.assertNotIn('review_rerun', self.state())
        self.assertEqual('ready', dispatch(self.request())['outcome'])

    def test_an_offline_future_choice_must_validate_before_execution(self):
        self.assertEqual('approval', dispatch(self.request())['outcome'])
        with patch('pr_review.models.discover', side_effect=ApiError('discovery_failed', 'Offline')) as discover:
            result = dispatch(self.request('--fix', 'codex-44', '--codex-44-model', 'unavailable'))
        self.assertEqual('approval', result['outcome'], result)
        discover.assert_not_called()
        refused = dispatch(self.request('--approve', '1'))
        self.assertEqual('invalid_request', refused['error']['code'], refused)
        self.assertEqual([], self.t.calls)
        self.assertEqual(1, len(self.t.assessments))
        corrected = dispatch(self.request('--codex-44-model', 'gpt-6.1-terra'))
        self.assertEqual('ready', corrected['outcome'], corrected)
        self.assertEqual(1, len(self.t.assessments))

    def test_a_new_request_supersedes_an_unapplied_settings_change(self):
        self.pause()
        name = self.state()['configuration']['_named']['fix']
        with patch('pr_review.issues.Workflow.bootstrap', side_effect=ApiError('model_failed', 'Stopped before attachment')):
            stopped = dispatch(self.request('--' + name + '-effort', 'medium'))
        self.assertEqual('model_failed', stopped['error']['code'])
        self.assertEqual('pending', self.state()['settings_transitions'][-1]['status'])
        self.assertEqual('ready', dispatch(self.request('--' + name + '-effort', 'low'))['outcome'])
        transitions = self.state()['settings_transitions']
        self.assertEqual(['superseded', 'applied'], [t['status'] for t in transitions])
        self.assertEqual(transitions[1]['id'], transitions[0]['superseded_by'])

    def test_changed_requirements_supersede_pending_regeneration_automatically(self):
        self.pause()
        with patch('pr_review.acceptance.review_issue', side_effect=ApiError('review_incomplete', 'Review stopped')):
            first = dispatch(self.request())
        self.assertEqual('review_incomplete', first['error']['code'])
        with patch('pr_review.issues.Workflow.fix', side_effect=ApiError('model_failed', 'Stopped after request was saved')):
            stopped = dispatch(self.request('--rerun-review', 'synthesis'))
        self.assertEqual('model_failed', stopped['error']['code'])
        self.assertEqual('pending', self.state()['review_rerun']['status'])
        self.t.f['issue']['body'] += ' Also retain history.'
        self.assertEqual('approval', dispatch(self.request())['outcome'])
        self.assertEqual('superseded', self.state()['review_rerun']['status'])
        result = dispatch(self.request('--approve', '2'))
        self.assertEqual('ready', result['outcome'], result)
        self.assertEqual('superseded', self.state()['review_rerun']['status'])

    def test_completed_work_remains_a_noop_after_changing_future_choices(self):
        self.pause()
        self.assertEqual('ready', dispatch(self.request())['outcome'])
        owner = owner_state.OwnerSession({'session': self.t.path})
        before = copy.deepcopy(owner.load()['batches'])
        name = self.state()['configuration']['_named']['fix']
        with patch('pr_review.models.discover', side_effect=ApiError('discovery_failed', 'Offline')) as discover:
            self.assertEqual('noop', dispatch(self.request('--' + name + '-effort', 'medium'))['outcome'])
        discover.assert_not_called()
        self.assertEqual(before, owner.load()['batches'])
        self.assertEqual('applied', self.state()['settings_transitions'][-1]['status'])
        self.assertEqual('medium', owner.load()['settings']['fix']['effort'])
        self.assertEqual('noop', dispatch(self.request())['outcome'])
        data = owner.load()
        data['settings']['fix']['effort'] = 'low'
        owner.save(data)
        self.assertEqual('noop', dispatch(self.request())['outcome'])
        self.assertEqual('low', self.state()['configuration']['fix']['effort'])
        self.assertEqual('low', owner.load()['settings']['fix']['effort'])
        self.assertEqual(before, owner.load()['batches'])
        self.assertEqual(1, len(self.t.assessments))

    def test_completed_no_change_rejects_regeneration_without_publication(self):
        def no_change(session, state, batch, role, context):
            if role == 'plan':
                return self.t.model(session, state, batch, role, context)
            return {'summary': 'Existing code meets the acceptance criteria',
                    'outcomes': [{'item': i['key'], 'status': 'already_addressed',
                                  'evidence': 'Verified existing code'} for i in context['feedback']],
                    'complete': True, 'checks': ['Focused verification'], 'blocking': []}
        self.assertEqual('approval', dispatch(self.request())['outcome'])
        with patch('pr_review.owner_providers.invoke', side_effect=no_change):
            result = dispatch(self.request('--approve', '1'))
        self.assertEqual('no_change', result['outcome'], result)
        owner = owner_state.OwnerSession({'session': self.t.path})
        data = owner.load()
        # Older no-commit completions need not have a commit transaction.
        data['batches'][-1].pop('commit_progress', None)
        data['batches'][-1].pop('commit_execution', None)
        owner.save(data)
        before = copy.deepcopy(data['batches'])
        calls = list(self.t.calls)
        result = dispatch(self.request('--rerun-review', 'synthesis'))
        self.assertEqual('invalid_request', result['error']['code'], result)
        self.assertEqual('complete', self.state()['phase'])
        self.assertNotIn('review_rerun', self.state())
        self.assertEqual(before, owner.load()['batches'])
        self.assertEqual(calls, self.t.calls)
        self.assertEqual('complete', dispatch(self.request())['outcome'])

    def test_new_explicit_selection_replaces_pending_regeneration(self):
        self.pause()
        with patch('pr_review.acceptance.review_issue', side_effect=ApiError('review_incomplete', 'Review stopped')):
            dispatch(self.request())
        with patch('pr_review.issues.Workflow.fix', side_effect=ApiError('model_failed', 'Stopped before application')):
            dispatch(self.request('--rerun-review', 'synthesis'))
            first = self.state()['review_rerun']
            dispatch(self.request())
            self.assertEqual(first, self.state()['review_rerun'])
            dispatch(self.request('--rerun-review', 'reviewer-1'))
            latest = self.state()['review_rerun']
            self.assertEqual(['reviewer-1'], latest['stages'])
            self.assertNotEqual(first['id'], latest['id'])
            dispatch(self.request('--rerun-review', 'reviewer-1'))
            self.assertEqual(latest, self.state()['review_rerun'])
        self.assertEqual('ready', dispatch(self.request())['outcome'])
        owner = owner_state.OwnerSession({'session': self.t.path}).load()
        self.assertEqual(latest['id'], owner['batches'][-1]['review_reruns']['reviewer-1'])
        self.assertEqual(latest['id'], owner['batches'][-1]['review_reruns']['synthesis'])
        self.assertNotIn(first['id'], owner['batches'][-1]['review_rerun_requests'])

    def test_new_selection_after_allocation_keeps_saved_work_and_applies_new_stages(self):
        self.pause()
        with patch('pr_review.acceptance.review_issue', side_effect=ApiError('review_incomplete', 'Review stopped')):
            dispatch(self.request())
        original = issue_settings.request_rerun
        def allocated(*args):
            original(*args)
            raise ApiError('model_failed', 'Interrupted after allocation')
        with patch('pr_review.issue_settings.request_rerun', side_effect=allocated):
            result = dispatch(self.request('--rerun-review', 'synthesis'))
        self.assertEqual('model_failed', result['error']['code'], result)
        first = self.state()['review_rerun']
        with patch('pr_review.issues.Workflow.fix', side_effect=ApiError('model_failed', 'Interrupted before review')):
            dispatch(self.request('--rerun-review', 'reviewer-1'))
        latest = self.state()['review_rerun']
        self.assertEqual('ready', dispatch(self.request())['outcome'])
        batch = owner_state.OwnerSession({'session': self.t.path}).load()['batches'][-1]
        self.assertEqual(latest['id'], batch['review_reruns']['reviewer-1'])
        self.assertEqual(latest['id'], batch['review_reruns']['synthesis'])
        self.assertIn(first['id'], batch['review_rerun_requests'])
        self.assertEqual(first['id'], self.state()['review_rerun_history'][-1]['id'])
        self.assertEqual('superseded', self.state()['review_rerun_history'][-1]['status'])

    def test_cached_settings_save_recovers_after_owner_write(self):
        self.pause()
        self.assertEqual('ready', dispatch(self.request())['outcome'])
        owner = owner_state.OwnerSession({'session': self.t.path})
        before = copy.deepcopy(owner.load()['batches'])
        name = self.state()['configuration']['_named']['fix']
        original = owner_state.OwnerSession.save
        def saved_then_stopped(instance, data):
            original(instance, data)
            raise ApiError('model_failed', 'Interrupted after saving future choices')
        with patch.object(owner_state.OwnerSession, 'save', new=saved_then_stopped):
            result = dispatch(self.request('--' + name + '-effort', 'medium'))
        self.assertEqual('model_failed', result['error']['code'], result)
        self.assertEqual('pending', self.state()['settings_transitions'][-1]['status'])
        self.assertEqual('medium', owner.load()['settings']['fix']['effort'])
        self.assertEqual('noop', dispatch(self.request())['outcome'])
        self.assertEqual('applied', self.state()['settings_transitions'][-1]['status'])
        self.assertEqual(before, owner.load()['batches'])
        self.assertEqual(1, len(self.state()['settings_transitions']))

    def test_completed_regeneration_receipt_recovers_before_completed_batch_guard(self):
        self.pause()
        with patch('pr_review.acceptance.review_issue', side_effect=ApiError('review_incomplete', 'Review stopped')):
            dispatch(self.request())
        self.assertEqual('ready', dispatch(self.request('--rerun-review', 'synthesis'))['outcome'])
        rerun = self.state()['review_rerun']
        # Model the Owner receipt surviving a crash before Workflow sees it.
        owner = owner_state.OwnerSession({'session': self.t.path})
        data = owner.load()
        data['batches'][-1]['review_reruns_applied'] = [rerun['id']]
        owner.save(data)
        calls = list(self.t.calls)
        self.assertEqual('noop', dispatch(self.request())['outcome'])
        self.assertEqual('complete', self.state()['review_rerun']['status'])
        self.assertEqual(calls, self.t.calls)


class ConversationSettingsTest(unittest.TestCase):
    def test_same_provider_reuses_session_with_new_model_and_effort(self):
        state = {'issue_workflow': 'workflow'}
        choice = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}
        key, session, _ = issue_settings.select_conversation(state, 'approved', choice)
        session.update(session_id='session-1', started=True)
        changed = {**choice, 'model': 'gpt-6.1-terra', 'effort': 'medium'}
        after, current, handoff = issue_settings.select_conversation(state, 'approved', changed)
        self.assertEqual(key, after)
        self.assertEqual('session-1', current['session_id'])
        self.assertIsNone(handoff)
        self.assertEqual(changed, current['choice'])

    def test_switching_provider_and_switching_back_start_fresh_conversations(self):
        state = {'issue_workflow': 'workflow'}
        a = {'provider': 'codex', 'model': 'sol', 'effort': 'high'}
        b = {'provider': 'claude', 'model': 'sonnet', 'effort': 'high'}
        keys = []
        for choice in (a, b, a):
            key, session, handoff = issue_settings.select_conversation(state, 'approved', choice)
            keys.append(key)
            self.assertNotIn('session_id', session)
            if len(keys) > 1:
                self.assertEqual(keys[-2], handoff['previous_session'])
            session.update(session_id='session-' + key, started=True)
        self.assertEqual(3, len(set(keys)))

    def test_legacy_session_migration_uses_provable_specification_and_provider(self):
        old = {'provider': 'claude', 'model': 'opus', 'effort': 'high'}
        key = digest(['workflow', 'approved', old])
        state = {'issue_workflow': 'workflow', 'issue_harness_sessions':
                 {key: {'choice': old, 'session_id': 'saved', 'started': True}}}
        after, session, _ = issue_settings.select_conversation(state, 'approved', {**old, 'model': 'sonnet'})
        self.assertEqual(key, after)
        self.assertEqual('saved', session['session_id'])
        fresh, _, _ = issue_settings.select_conversation(state, 'new specification', old)
        self.assertNotEqual(key, fresh)


class ReviewSettingsTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issue_engine.IssueEngineTest()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def paired(self):
        t = self.t
        data = t.session.load()
        data['settings']['final_review'] = {'reviewer_slots': copy.deepcopy(t.SLOTS),
                                          'synthesis': dict(t.SYNTHESIS), 'synthesis_tests': 'focused'}
        t.session.save(data)

    def test_partial_review_preserves_completed_peer_under_its_original_choice(self):
        t = self.t
        self.paired()
        with patch.dict('os.environ', {'FAIL_STAGES': 'reviewer-2'}), self.assertRaises(ApiError):
            t.start()
        before = t.last()
        data = t.session.load()
        for slot in data['settings']['final_review']['reviewer_slots']:
            slot['effort'] = 'medium'
        data['settings']['final_review']['synthesis']['effort'] = 'medium'
        t.session.save(data)
        view = t.start(batch=before['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(1, len(t.turns))
        self.assertEqual(1, t.counts()['reviewer-1'])
        attempt = t.last()['attempts'][-1]
        self.assertEqual('high', attempt['choice']['reviewer_slots'][0]['effort'])
        self.assertEqual('medium', attempt['choice']['reviewer_slots'][1]['effort'])
        self.assertEqual('medium', attempt['choice']['synthesis']['effort'])

    def test_synthesis_regeneration_preserves_peer_and_archives_the_old_attempt(self):
        t = self.t
        t.verdict('incomplete', summary='Check unavailable')
        with self.assertRaises(ApiError):
            t.start()
        before = t.last()
        t.verdict()
        result = t.start(batch=before['id'], rerun_review=['synthesis'], review_rerun_id='explicit-rerun')
        self.assertEqual('ready', result['batches'][-1]['status'])
        self.assertEqual(1, len(t.turns))
        self.assertEqual(2, t.counts()['synthesis'])
        self.assertIn('explicit-rerun', t.last()['review_reruns_applied'])

    def test_explicit_completed_reviewer_rerun_retains_the_other_peer(self):
        t = self.t
        self.paired()
        t.verdict('incomplete', summary='Check unavailable')
        with self.assertRaises(ApiError):
            t.start()
        before = t.last()
        data = t.session.load()
        data['settings']['final_review']['reviewer_slots'][0]['effort'] = 'medium'
        t.session.save(data)
        t.verdict()
        view = t.start(batch=before['id'], rerun_review=['reviewer-1'], review_rerun_id='rerun-peer')
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(2, t.counts()['reviewer-1'])
        self.assertEqual(1, t.counts()['reviewer-2'])
        self.assertEqual(2, t.counts()['synthesis'])
        self.assertEqual(1, len(t.turns))
        self.assertEqual('medium', t.last()['attempts'][-1]['choice']['reviewer_slots'][0]['effort'])
        self.assertTrue(list(t.workspace().glob('restart-history/*/phases.json')))

    def test_invalid_completed_peer_is_rerun_under_the_new_choice(self):
        t = self.t
        self.paired()
        with patch.dict('os.environ', {'FAIL_STAGES': 'reviewer-2'}), self.assertRaises(ApiError):
            t.start()
        before = t.last()
        (t.workspace() / 'reviews/reviewer-1.md').write_text('damaged report')
        data = t.session.load()
        data['settings']['final_review']['reviewer_slots'][0]['effort'] = 'medium'
        t.session.save(data)
        view = t.start(batch=before['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(2, t.counts()['reviewer-1'])
        self.assertEqual('medium', t.last()['attempts'][-1]['choice']['reviewer_slots'][0]['effort'])

    def test_completed_synthesis_keeps_its_original_settings_on_resume(self):
        t = self.t
        t.start()
        before = t.last()
        data = t.session.load()
        final = data['settings']['final_review']
        final['reviewer_slots'][0]['effort'] = 'medium'
        final['synthesis']['effort'] = 'medium'
        t.session.save(data)
        view = t.start(batch=before['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])
        self.assertEqual(1, t.counts()['synthesis'])
        self.assertEqual(1, len(t.turns))
        self.assertEqual('high', t.last()['attempts'][-1]['choice']['synthesis']['effort'])
