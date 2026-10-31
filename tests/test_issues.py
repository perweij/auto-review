"""Issue eligibility, publication recovery, and real Owner integration."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.common import ApiError, digest
from pr_review.issue_eligibility import evaluate_eligibility, EligibilityPolicy, collect_eligibility_facts
from pr_review.issues import Workflow, approval_comment, dispatch, execution_complete, input_digest
from pr_review.issue_writes import Writes
from pr_review import owner_import, owner_state
import test_owner


def facts():
    return {'complete': True, 'repository': 'github.com/org/repo', 'viewer': 'author', 'permission': True,
            'issue': {'number': 12, 'title': 'Fix behavior', 'body': 'The behavior should be fixed.', 'state': 'open',
                      'assignees': [], 'html_url': 'https://github.com/org/repo/issues/12'},
            'pr': None, 'pulls': [], 'comments': [], 'base_branch': 'main', 'base': 'base', 'head': None, 'untouched': True}


class EligibilityTest(unittest.TestCase):
    def test_scanner_and_execution_same_facts(self):
        f = facts()
        self.assertEqual(evaluate_eligibility(f), evaluate_eligibility(collect_eligibility_facts({}, None, f)))
        self.assertEqual('eligible', evaluate_eligibility(f)['eligibility'])

    def test_unknown_permission_and_incomplete_data_fail_closed(self):
        for f in ({'complete': False}, {**facts(), 'permission': None}):
            self.assertEqual('indeterminate', evaluate_eligibility(f)['eligibility'])

    def test_force_only_overrides_assignment(self):
        f = facts()
        f['issue']['assignees'] = [{'login': 'other'}]
        self.assertEqual('ineligible', evaluate_eligibility(f)['eligibility'])
        self.assertEqual('eligible', evaluate_eligibility(f, EligibilityPolicy(True))['eligibility'])
        f['issue']['body'] = ''
        self.assertEqual('ineligible', evaluate_eligibility(f, EligibilityPolicy(True))['eligibility'])

    def test_self_plus_other_fails(self):
        f = facts()
        f['issue']['assignees'] = [{'login': 'author'}, {'login': 'other'}]
        self.assertEqual('ineligible', evaluate_eligibility(f)['eligibility'])

    def test_approval_exact_author_revision_and_time(self):
        state = {'viewer': 'author', 'plan': {'revision': 2, 'published_at': '2026-01-02'}, 'actions': {}}
        good = {'user': {'login': 'author'}, 'body': '/fix-issue approve 2', 'created_at': '2026-01-03'}
        self.assertTrue(approval_comment(good, state))
        for bad in ({**good, 'body': '> /fix-issue approve 2'}, {**good, 'body': '/fix-issue approve 1'},
                    {**good, 'user': {'login': 'other'}}, {**good, 'created_at': '2026-01-01'}):
            self.assertFalse(approval_comment(bad, state))


class CommentInputTest(unittest.TestCase):
    def setUp(self):
        self.state = {'viewer': 'author', 'actions': {},
                      'plan': {'revision': 2, 'published_at': '2026-01-02'}}
        self.comment = {'id': 10, 'user': {'login': 'author', 'avatar_url': 'old'},
                        'body': 'Keep the new behavior.', 'created_at': '2026-01-03',
                        'reactions': {'total_count': 0}}

    def identity(self, comment):
        return input_digest({**facts(), 'comments': [comment]}, self.state)

    def test_metadata_is_ignored_but_body_and_author_changes_are_detected(self):
        original = self.identity(self.comment)
        changed = copy.deepcopy(self.comment)
        changed.update(reactions={'total_count': 1}, updated_at='tomorrow', html_url='new')
        changed['user']['avatar_url'] = 'new'
        self.assertEqual(original, self.identity(changed))
        for field in ({'body': 'Changed requirement'}, {'user': {'login': 'other'}}, {'id': 11}):
            self.assertNotEqual(original, self.identity({**changed, **field}))

    def test_only_standalone_commands_by_initiator_are_excluded(self):
        empty = input_digest(facts(), self.state)
        self.assertEqual(empty, self.identity({**self.comment, 'body': ' /fix-issue approve 2\n'}))
        for body in ('/fix-issue approve 2\nAlso retain history.', '/fix-issue approve 2 please',
                     '/fix-issue approve nope', '> /fix-issue approve 2'):
            for author in ('author', 'other'):
                with self.subTest(body=body, author=author):
                    comment = {**self.comment, 'body': body, 'user': {'login': author}}
                    self.assertNotEqual(empty, self.identity(comment))
                    self.assertFalse(approval_comment(comment, self.state))
        foreign = {**self.comment, 'body': '/fix-issue approve 2', 'user': {'login': 'other'}}
        self.assertNotEqual(empty, self.identity(foreign))
        self.assertFalse(approval_comment(foreign, self.state))

    def test_receipts_exclude_exact_publications_and_validate_approval(self):
        receipt = {**self.comment, 'body': '/fix-issue approve 2\n\n<!-- workflow marker -->'}
        self.state['actions']['approve:2'] = {'body': '/fix-issue approve 2', 'approval': True,
                                              'receipt': receipt}
        self.assertTrue(approval_comment(receipt, self.state))
        self.assertEqual(input_digest(facts(), self.state), self.identity(receipt))
        edited = {**receipt, 'body': receipt['body'] + '\nAlso retain history.'}
        self.assertFalse(approval_comment(edited, self.state))
        self.assertNotEqual(input_digest(facts(), self.state), self.identity(edited))
        foreign = {**receipt, 'user': {'login': 'other'}}
        self.assertNotEqual(input_digest(facts(), self.state), self.identity(foreign))

    def test_imported_answers_are_stable_and_keep_edited_publications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SimpleNamespace(identity='github.com/org/repo', cwd=root, common=root)
            f = {**facts(), 'comments': [self.comment]}
            with patch('pr_review.issues.state_root', return_value=root):
                flow = Workflow({'pr': 7}, repo, f)
            flow.guard = Mock()
            flow.state.update(plan={**self.state['plan'], 'body': 'Plan'}, remote_branch='feature', configuration={})
            data = {'feedback': [], 'batches': [], 'settings': {}}
            owner = SimpleNamespace(path=root / 'owner', execution_lock=root / 'owner.lock',
                                    load=lambda: data, save=lambda value: None)
            with patch('pr_review.issues.owner_state.open_session', return_value={'session': str(owner.path)}), \
                    patch('pr_review.issues.owner_state.OwnerSession', return_value=owner):
                flow.bootstrap()
                original = copy.deepcopy(data['feedback'][0])
                data['feedback'][0].update(attention=False, queued=False, triage='handled')
                self.comment['reactions']['total_count'] = 1
                self.comment['user']['avatar_url'] = 'new'
                flow.bootstrap()
                self.assertEqual(original['fingerprint'], data['feedback'][0]['fingerprint'])
                self.assertFalse(data['feedback'][0]['attention'])
                self.assertEqual([{'id': 10, 'user': {'login': 'author'}, 'body': self.comment['body']}],
                                 data['feedback'][0]['source']['context']['answers'])
                flow.state['actions']['question'] = {'body': 'Question', 'receipt': copy.deepcopy(self.comment)}
                self.comment['body'] = '/fix-issue approve 2\nAlso retain history.'
                flow.bootstrap()
                self.assertNotEqual(original['fingerprint'], data['feedback'][0]['fingerprint'])
                self.assertEqual(self.comment['body'], data['feedback'][0]['source']['context']['answers'][0]['body'])


class ExecutionCompleteTest(unittest.TestCase):
    def test_replacement_verification_cannot_hide_execution_or_freshness_blockers(self):
        from pr_review.issues import check_owner_execution
        old = {'id': 'old', 'items': ['issue:12'], 'status': 'ready', 'discussion_stale': True,
               'review': {'complete': True, 'blocking': []}, 'commit_progress': {'complete': True},
               'commit_plan': {'units': [{'id': 'fix'}]}, 'commit_execution': {'status': 'complete'}}
        new = {'id': 'new', 'items': ['issue:12'], 'status': 'ready',
               'review': {'complete': True, 'blocking': []}, 'commit_plan': {'units': []},
               'verified_items': ['issue:12'], 'verified_fingerprints': {'issue:12': 'current'},
               'verified_base': 'base'}
        data = {'batches': [old, new], 'metadata': {'baseRefOid': 'base'},
                'feedback': [{'key': 'issue:12', 'fingerprint': 'current'}]}
        with patch('pr_review.issues.owner_git.validate'), patch('pr_review.issues.owner_git.clean'):
            check_owner_execution(SimpleNamespace(path='owner'), data)
            cases = [({'commit_execution': {'status': 'paused', 'inspection_required': True}}, {}),
                     ({'commit_execution': {'status': 'paused'}}, {}),
                     ({'integrity_failure': 'Protected state changed'}, {}),
                     ({}, {'review': {'complete': False, 'blocking': []}}),
                     ({}, {'verified_items': []}),
                     ({}, {'verified_fingerprints': {'issue:12': 'old'}}),
                     ({}, {'verified_base': 'old-base'})]
            for old_changes, new_changes in cases:
                with self.subTest(old=old_changes, new=new_changes):
                    changed = {**data, 'batches': [{**old, **old_changes}, {**new, **new_changes}]}
                    with self.assertRaises(ApiError) as caught:
                        check_owner_execution(SimpleNamespace(path='owner'), changed)
                    self.assertEqual('needs_attention', caught.exception.code)

    def test_owner_blockers_override_complete_progress_and_empty_plans(self):
        for units in ([], [{'id': 'fix'}]):
            batch = {'status': 'ready', 'review': {'complete': True, 'blocking': []},
                     'commit_progress': {'complete': True}, 'commit_plan': {'units': units},
                     'commit_execution': {'status': 'complete'}}
            self.assertTrue(execution_complete(batch))
            for blocker in ({'commit_execution': {'status': 'paused', 'inspection_required': True}},
                            {'commit_execution': {'status': 'paused'}},
                            {'integrity_failure': 'Protected state changed'},
                            {'discussion_stale': True}):
                with self.subTest(units=units, blocker=blocker):
                    self.assertFalse(execution_complete({**batch, **blocker}))


class EligibilityRelationshipTest(unittest.TestCase):
    def setUp(self):
        self.repo = SimpleNamespace(identity='github.com/org/repo', name='org/repo', cwd=Path('.'),
                                    remote='origin', git=Mock(side_effect=self.git))
        self.pulls = {7: {'number': 7, 'state': 'open', 'assignees': [],
                          'head': {'sha': 'head', 'repo': {'full_name': 'org/repo'}},
                          'base': {'ref': 'main'}}}
        self.linked = []
        for target, kwargs in [
                ('pr_review.issue_eligibility.github_json', {'side_effect': self.github}),
                ('pr_review.issue_eligibility.connection', {'side_effect': self.connection}),
                ('pr_review.issue_eligibility.command', {'side_effect': AssertionError('Unexpected command')})]:
            p = patch(target, **kwargs)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith('github_json'):
                self.remote_reads = mocked

    def git(self, *args):
        if args[0] == 'rev-parse':
            return SimpleNamespace(stdout='head' if args[1].endswith('/head') else 'base')
        return SimpleNamespace(stdout='')

    def github(self, identity, endpoint, cwd, pages=False):
        if pages:
            return [[]]
        if endpoint == 'user':
            return {'login': 'author'}
        if endpoint == 'repos/org/repo':
            return {'default_branch': 'main', 'permissions': {'push': True}}
        if endpoint == 'repos/org/repo/issues/12':
            return copy.deepcopy(facts()['issue'])
        return copy.deepcopy(self.pulls[int(endpoint.rsplit('/', 1)[1])])

    def connection(self, repo, number, *, issue=True):
        return copy.deepcopy(self.linked if issue else [self.ref(12)])

    @staticmethod
    def ref(number, repository='org/repo'):
        return {'number': number, 'repository': {'nameWithOwner': repository}}

    def test_foreign_only_and_mixed_associations_are_rejected(self):
        foreign = self.ref(7, 'other/repo')
        for linked in ([foreign], [self.ref(7), foreign]):
            for target in ({'issue': 12}, {'pr': 7}):
                with self.subTest(linked=linked, target=target):
                    self.linked = linked
                    f = collect_eligibility_facts(target, self.repo)
                    self.assertTrue(f['complete'], f)
                    self.assertEqual(linked, f['pull_references'])
                    for force in (False, True):
                        result = evaluate_eligibility(f, EligibilityPolicy(force))
                        self.assertEqual('ineligible', result['eligibility'])
                        check = next(c for c in result['checks'] if c['code'] == 'foreign_pr_unsupported')
                        self.assertEqual('fail', check['outcome'])
                        self.assertFalse(check['overridable'])
                        self.assertFalse(check['overridden'])
                    self.repo.git.assert_not_called()
        self.assertFalse(any('/pulls/' in call.args[1] for call in self.remote_reads.call_args_list))

    def test_no_association_and_same_repository_associations_remain_eligible(self):
        for linked in ([], [self.ref(7)], [self.ref(7, 'Org/Repo')]):
            with self.subTest(linked=linked):
                self.linked = linked
                f = collect_eligibility_facts({'issue': 12}, self.repo)
                self.assertTrue(f['complete'], f)
                self.assertEqual(linked, f['pull_references'])
                self.assertEqual('eligible', evaluate_eligibility(f)['eligibility'])
                self.assertEqual(7 if linked else None, (f['pr'] or {}).get('number'))

    def test_local_ambiguity_and_prior_merge_still_block(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                self.pulls[8] = {**copy.deepcopy(self.pulls[7]), 'number': 8,
                                 'state': 'closed' if merged else 'open',
                                 'merged_at': '2026-10-30' if merged else None}
                self.linked = [self.ref(7), self.ref(8)]
                f = collect_eligibility_facts({'issue': 12}, self.repo)
                self.assertTrue(f['complete'], f)
                result = evaluate_eligibility(f)
                self.assertEqual('ineligible', result['eligibility'])
                code = 'prior_merged_pr' if merged else 'multiple_open_prs'
                self.assertTrue(any(c['code'] == code and c['outcome'] == 'fail' for c in result['checks']))


class ReadinessCacheTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = SimpleNamespace(identity='github.com/org/repo', common=root, cwd=root,
                               git=Mock(return_value=SimpleNamespace(stdout='head\n')))
        self.f = {**facts(), 'head': 'head'}
        owner = SimpleNamespace(execution_lock=root / 'owner.lock', repo=SimpleNamespace(lock=root / 'repo.lock', git=repo.git),
                                load=lambda: {'worktree': str(root), 'feedback': [], 'batches': [], 'actions': [], 'replies': []})
        assessment = {'decision': 'plan', 'summary': 'Plan', 'plan': 'Implement the fix.',
                      'acceptance': ['Correct behavior'], 'questions': []}
        for target, effect in [('pr_review.issues.state_root', lambda: root),
                               ('pr_review.issues.collect_eligibility_facts', lambda *a: copy.deepcopy(self.f)),
                               ('pr_review.issues.assess_issue', lambda *a: copy.deepcopy(assessment)),
                               ('pr_review.issues.owner_git.validate', lambda *a: None),
                               ('pr_review.issues.owner_git.clean', lambda *a: None),
                               ('pr_review.issues.owner_state.refresh', lambda *a: None)]:
            p = patch(target, side_effect=effect)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith('assess_issue'):
                self.assess = mocked
        self.flow = Workflow({'pr': 7, 'yolo': True}, repo, self.f)
        self.flow.state['remote_head'] = 'head'
        self.flow.workspace = Mock(return_value=root)
        self.flow.clean = Mock()
        self.flow.writes = Mock()
        self.flow.writes.comment.return_value = {'created_at': '2026-10-30'}
        self.flow.feedback_context = Mock(return_value=[])
        self.flow.bootstrap = Mock(return_value=owner)
        self.reviewed_bases = []
        self.flow.review = Mock(side_effect=lambda: self.reviewed_bases.append(self.flow.facts['base']) or [])
        self.flow.plan = Mock(wraps=self.flow.plan)
        self.assessment = assessment

    def test_unchanged_snapshot_remains_noop(self):
        self.assertEqual('ready', self.flow.run()['outcome'])
        saved = json.loads(self.flow.file.read_text())
        self.assertEqual('base', saved['ready_base'])
        self.assertEqual('noop', self.flow.run()['outcome'])
        self.assertEqual(1, self.flow.plan.call_count)
        self.assertEqual(1, self.assess.call_count)
        self.assertEqual(['base'], self.reviewed_bases)

    def test_comment_metadata_keeps_plan_guard_and_readiness_stable(self):
        self.f['comments'] = [{'id': 10, 'user': {'login': 'human', 'avatar_url': 'old'},
                               'body': 'Keep history.', 'reactions': {'total_count': 0}}]
        self.assertEqual('ready', self.flow.run()['outcome'])
        context = self.assess.call_args.args[0]
        self.assertEqual([{'id': 10, 'user': {'login': 'human'}, 'body': 'Keep history.'}], context['comments'])
        self.f['comments'][0]['reactions']['total_count'] = 1
        self.f['comments'][0]['user']['avatar_url'] = 'new'
        self.flow.state['phase'] = 'working'
        self.flow.guard()
        self.flow.state['phase'] = 'ready'
        self.assertEqual('noop', self.flow.run()['outcome'])
        self.assertEqual(1, self.assess.call_count)

    def test_approval_prefix_requirement_during_execution_blocks_and_reassesses(self):
        def review_then_add_requirement():
            self.f['comments'].append({'id': 10, 'user': {'login': 'other'},
                                       'body': '/fix-issue approve 1\nAlso retain history.'})
            return []

        self.flow.review.side_effect = review_then_add_requirement
        with self.assertRaises(ApiError) as caught:
            self.flow.run()
        self.assertEqual('stale_discussion', caught.exception.code)
        self.assertNotEqual('ready', self.flow.state['phase'])
        self.flow.review.side_effect = lambda: []
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(self.f['comments'][0]['body'], self.assess.call_args.args[0]['comments'][0]['body'])
        self.assertEqual('noop', self.flow.run()['outcome'])

    def test_approval_prefix_requirement_invalidates_cached_readiness(self):
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.f['comments'].append({'id': 10, 'user': {'login': 'author'},
                                   'body': '/fix-issue approve 1\nAlso retain history.'})
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.assertEqual(2, self.assess.call_count)

    def test_pr_feedback_ignores_metadata_but_keeps_review_context(self):
        self.flow.repo.name = 'org/repo'
        comment = {'id': 20, 'user': {'login': 'reviewer', 'avatar_url': 'old'}, 'body': 'Fix this.',
                   'path': 'source.txt', 'line': 1, 'commit_id': 'head', 'state': 'CHANGES_REQUESTED',
                   'reactions': {'total_count': 0}}
        with patch('pr_review.issues.pages', return_value=[comment]):
            original = digest(Workflow.feedback_context(self.flow))
            comment['reactions']['total_count'] = 1
            comment['user']['avatar_url'] = 'new'
            self.assertEqual(original, digest(Workflow.feedback_context(self.flow)))
            for field, value in [('body', 'New request'), ('line', 2), ('state', 'DISMISSED')]:
                old = comment[field]
                comment[field] = value
                self.assertNotEqual(original, digest(Workflow.feedback_context(self.flow)))
                comment[field] = old

    def test_base_only_change_reassesses_and_reviews(self):
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.f['base'] = 'new-base'
        self.assessment['decision'] = 'continue'
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(['base', 'new-base'], self.reviewed_bases)
        saved = json.loads(self.flow.file.read_text())
        self.assertEqual('head', saved['ready_head'])
        self.assertEqual('new-base', saved['assessed_base'])
        self.assertEqual('new-base', saved['ready_base'])
        self.assertEqual(2, saved['plan']['revision'])
        self.assertEqual('noop', self.flow.run()['outcome'])

    def test_missing_cached_base_revalidates_readiness(self):
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.flow.state.pop('ready_base', None)
        self.flow.save()
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.assertEqual(2, self.flow.plan.call_count)
        self.assertEqual(['base', 'base'], self.reviewed_bases)
        self.assertEqual('base', json.loads(self.flow.file.read_text())['ready_base'])
        self.assertEqual('noop', self.flow.run()['outcome'])

    def test_base_moves_after_review_blocks_readiness_and_reassesses_on_retry(self):
        def review_then_move_base():
            self.reviewed_bases.append(self.flow.facts['base'])
            self.f['base'] = 'new-base'
            return []

        self.flow.review.side_effect = review_then_move_base
        with self.assertRaises(ApiError) as caught:
            self.flow.run()
        self.assertEqual('stale_base', caught.exception.code)
        self.assertNotEqual('ready', self.flow.state['phase'])
        self.assertNotIn('ready_base', self.flow.state)
        self.assertEqual(['base'], self.reviewed_bases)

        self.flow.review.side_effect = lambda: self.reviewed_bases.append(self.flow.facts['base']) or []
        self.assessment['decision'] = 'continue'
        self.assertEqual('ready', self.flow.run()['outcome'])
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(['base', 'new-base'], self.reviewed_bases)
        self.assertEqual('new-base', json.loads(self.flow.file.read_text())['ready_base'])
        self.assertEqual('noop', self.flow.run()['outcome'])

    def test_base_moves_during_ready_comment_blocks_readiness(self):
        def comment(body, key):
            if key.startswith('ready:'):
                self.f['base'] = 'new-base'
            return {'created_at': '2026-10-30'}

        self.flow.writes.comment.side_effect = comment
        with self.assertRaises(ApiError) as caught:
            self.flow.run()
        self.assertEqual('stale_base', caught.exception.code)
        self.assertEqual('base', self.flow.state['assessed_base'])
        self.assertEqual('new-base', self.flow.facts['base'])
        self.assertNotIn('ready_base', self.flow.state)
        self.assertNotEqual('ready', self.flow.state['phase'])

    def test_review_snapshot_must_match_assessed_base(self):
        self.assertEqual('ready', self.flow.run()['outcome'])
        session = SimpleNamespace(root=self.flow.path / 'review', load=lambda: {'comments': []})
        with patch('pr_review.reviews.execute', return_value=session), \
                patch('pr_review.automation.manual_reason', return_value=None):
            session.manifest = {'snapshot': {'base': 'base', 'head': 'head'}}
            self.assertEqual([], Workflow.review(self.flow))
            session.manifest = {'snapshot': {'base': 'new-base', 'head': 'head'}}
            with self.assertRaises(ApiError) as caught:
                Workflow.review(self.flow)
            self.assertEqual('stale_base', caught.exception.code)

            session.manifest = {'snapshot': {'base': 'base', 'head': 'old-head'}}
            with self.assertRaises(ApiError) as caught:
                Workflow.review(self.flow)
            self.assertEqual('stale_head', caught.exception.code)


class ReplyObligationTest(unittest.TestCase):
    def test_absent_reply_blocks_retries_until_explicit_owner_publication(self):
        t = IssueIntegrationTest()
        self.addCleanup(t.doCleanups)
        t.setUp()
        t.feedback.append(t.item('thread:T1', 101))
        request = {**t.req, 'issue': None, 'pr': 7, 'yolo': True}
        attempts = []

        def lose_response(*args, **kwargs):
            if kwargs.get('pages'):
                return []
            attempts.append(args)
            raise ApiError('network', 'Reply was not stored; response lost')

        with patch.object(Writes, 'push', autospec=True, side_effect=t.simulate_push), \
                patch('pr_review.owner_publication.gh', side_effect=lose_response):
            self.assertEqual('uncertain', dispatch(request)['outcome'])
            for _ in range(3):
                result = dispatch(request)
                self.assertEqual('needs_attention', result['outcome'], result)
                owner = owner_state.OwnerSession({'session': t.path})
                data = owner.load()
                self.assertEqual('unsent', data['actions'][0]['status'])
                self.assertEqual('unstaged', data['replies'][0]['status'])
                self.assertTrue(t.f['pr']['draft'])
                self.assertFalse(any(k.startswith('ready:') for k in Workflow(request, t.repo_object, t.f).state['actions']))
            self.assertEqual(1, len(attempts))
        self.assertFalse(t.posts)
        # Explicit staging/publication resolves the reply, while the absent
        # attempt remains in the journal as historical evidence.
        t.request('owner-mutate', operation='stage', reply=data['replies'][0]['id'])
        prepared = t.request('owner-prepare')
        t.request('owner-publish', token=prepared['token'])
        self.assertEqual('unsent', owner.load()['actions'][0]['status'])
        self.assertEqual('published', owner.load()['replies'][0]['status'])
        with patch.object(Writes, 'push', autospec=True, side_effect=t.simulate_push):
            self.assertEqual('ready', dispatch(request)['outcome'])
            self.assertEqual('noop', dispatch(request)['outcome'])
        self.assertEqual(1, len(t.posts))

    def test_cached_readiness_checks_persistent_reply_obligations(self):
        t = ReadinessCacheTest()
        self.addCleanup(t.doCleanups)
        t.setUp()
        self.assertEqual('ready', t.flow.run()['outcome'])
        owner = t.flow.bootstrap.return_value
        data = owner.load()
        owner.load = lambda: data
        t.flow.state['owner'] = str(t.flow.path / 'owner')
        published = {'id': 'reply', 'source': 'thread:T1', 'batch': 'fix', 'status': 'published', 'automatic': True}
        batch = {'id': 'fix', 'items': [], 'status': 'ready', 'review': {'complete': True, 'blocking': []},
                 'commit_plan': {'units': []}}
        cases = [({'actions': [{'reply': 'reply', 'status': 'unsent'}]}, 'Recovered unsent'),
                 ({'actions': [{'reply': 'reply', 'status': 'ambiguous'}]}, 'Uncertain replies'),
                 ({'replies': [{**published, 'status': 'unstaged'}]}, 'Automatic replies'),
                 ({'replies': [{**published, 'status': 'staged'}]}, 'Automatic replies'),
                 ({'replies': [{**published, 'status': 'staged', 'automatic': False}]}, 'Manual staged'),
                 ({'replies': [{**published, 'status': 'staged', 'needs_attention': 'Inspect context'}]}, 'Inspect context'),
                 ({'batches': [{**batch, 'reply_issues': [{'item': 'thread:T1', 'reason': 'Destination missing'}]}]}, 'Destination missing')]
        with patch('pr_review.issues.owner_state.OwnerSession', return_value=owner):
            for update, message in cases:
                with self.subTest(update=update):
                    data.update(actions=[], replies=[], batches=[])
                    data.update(copy.deepcopy(update))
                    with self.assertRaises(ApiError) as caught:
                        t.flow.run()
                    self.assertEqual('needs_attention', caught.exception.code)
                    self.assertIn(message, str(caught.exception))
            # A successful explicit retry supersedes the historical unsent
            # attempt and its earlier drafting blocker.
            data.update(actions=[{'reply': 'reply', 'status': 'unsent'}], replies=[published],
                        batches=[{**batch, 'reply_issues': [{'item': 'thread:T1', 'reason': 'Recover reply'}]}])
            self.assertEqual('noop', t.flow.run()['outcome'])

    def test_active_run_blocks_unstaged_replies_and_missing_drafts(self):
        t = ReadinessCacheTest()
        self.addCleanup(t.doCleanups)
        t.setUp()
        owner = t.flow.bootstrap.return_value
        data = owner.load()
        owner.load = lambda: data
        reply = {'id': 'reply', 'source': 'thread:T1', 'status': 'unstaged', 'automatic': True}
        batch = {'id': 'fix', 'items': [], 'status': 'ready', 'review': {'complete': True, 'blocking': []},
                 'commit_plan': {'units': []},
                 'reply_issues': [{'item': 'thread:T1', 'reason': 'Cannot create required reply'}]}
        for update in ({'replies': [reply]}, {'batches': [batch]}):
            with self.subTest(update=update):
                data.update(actions=[], replies=[], batches=[])
                t.flow.writes.reset_mock()
                t.flow.review.side_effect = lambda: data.update(copy.deepcopy(update)) or []
                with patch('pr_review.issues.owner_publication.prepare') as prepare, \
                        patch('pr_review.issues.owner_publication.publish') as publish:
                    for _ in range(2):
                        with self.assertRaises(ApiError) as caught:
                            t.flow.run()
                        self.assertEqual('needs_attention', caught.exception.code)
                        self.assertNotEqual('ready', t.flow.state['phase'])
                        # Inject at the review boundary again on the next run;
                        # this fixture isolates the publication/readiness gate.
                        data.update(actions=[], replies=[], batches=[])
                    prepare.assert_not_called()
                    publish.assert_not_called()
                self.assertNotIn(unittest.mock.call(False), t.flow.writes.draft.call_args_list)
                self.assertFalse(any(call.args[1].startswith('ready:') for call in t.flow.writes.comment.call_args_list))


class QuestionPublicationRecoveryTest(unittest.TestCase):
    """Exercise durable workflow retries with all Git and remote writes mocked."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = SimpleNamespace(identity='github.com/org/repo', name='org/repo',
                                    common=self.root, cwd=self.root,
                                    git=Mock(return_value=SimpleNamespace(stdout='head\n')))
        self.f = {**facts(), 'head': 'head'}
        self.req = {'command': 'issue-run', 'issue': 12}
        self.assessment = {'decision': 'questions', 'summary': 'Need details',
                           'questions': ['Which behavior is intended?'], 'plan': '', 'acceptance': []}
        self.remote = []
        self.lost = True
        self.store = True
        self.posts = 0
        for target, kwargs in [
                ('pr_review.issues.state_root', {'return_value': self.root}),
                ('pr_review.issues.resolve', {'return_value': self.repo}),
                ('pr_review.issues.collect_eligibility_facts', {'side_effect': self.collect}),
                ('pr_review.issues.assess_issue', {'side_effect': lambda *a: copy.deepcopy(self.assessment)}),
                ('pr_review.issues.Workflow.workspace', {'return_value': self.root}),
                ('pr_review.issues.Workflow.clean', {}),
                ('pr_review.issue_writes.pages', {'side_effect': lambda *a: copy.deepcopy(self.remote)}),
                ('pr_review.issue_writes.Writes.api', {'side_effect': self.post})]:
            p = patch(target, **kwargs)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith('assess_issue'):
                self.assess = mocked

    def collect(self, *args):
        return {**copy.deepcopy(self.f), 'comments': copy.deepcopy(self.remote)}

    def post(self, endpoint, payload):
        self.posts += 1
        if self.store:
            self.remote.append({'id': self.posts, 'body': payload['body'], 'user': {'login': 'author'},
                                'created_at': '2026-10-30'})
        if self.lost:
            raise ApiError('network', 'Lost response')
        return self.remote[-1]

    def saved(self):
        return Workflow(self.req, self.repo, self.f).state

    def test_accepted_question_recovers_before_assessment_identity(self):
        self.assertEqual('uncertain', dispatch(self.req)['outcome'])
        original = self.saved()['input_digest']
        for _ in range(2):
            self.assertEqual('answers', dispatch(self.req)['outcome'])
        saved = self.saved()
        self.assertEqual(1, self.posts)
        self.assertEqual(1, self.assess.call_count)
        self.assertEqual(original, saved['input_digest'])
        self.assertEqual(original, saved['applied_assessment'])
        self.assertEqual(1, saved['question_round'])
        self.assertEqual(['questions:' + original], list(saved['actions']))
        self.assertEqual(self.remote[0], saved['actions']['questions:' + original]['receipt'])

    def test_absent_plan_requires_explicit_recovery_then_resumes_same_revision(self):
        self.assessment.update(decision='plan', questions=[], plan='Implement behavior.', acceptance=['Correct output'])
        self.store = False
        for _ in range(3):
            self.assertEqual('uncertain', dispatch(self.req)['outcome'])
        self.assertEqual(1, self.posts)
        self.assertEqual(1, self.assess.call_count)
        inspected = dispatch({**self.req, 'command': 'issue-recover'})
        self.assertEqual('absent', inspected['actions']['plan:1']['readback']['outcome'])
        self.assertEqual('submitting', self.saved()['actions']['plan:1']['status'])
        result = dispatch({**self.req, 'command': 'issue-recover', 'retry_absent': 'plan:1'})
        self.assertEqual('recovered', result['outcome'])
        saved = self.saved()['actions']['plan:1']
        self.assertEqual('pending', saved['status'])
        self.assertEqual('submitting', saved['history'][0]['previous']['status'])
        self.assertEqual(1, self.posts)
        self.store, self.lost = True, False
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(2, self.posts)
        self.assertEqual(1, self.saved()['plan']['revision'])
        self.assertEqual(1, self.assess.call_count)
        self.assertEqual('complete', self.saved()['actions']['plan:1']['status'])


class BlockerReassessmentTest(unittest.TestCase):
    # Reuse the in-memory publication fixture without inheriting its tests.
    collect = QuestionPublicationRecoveryTest.collect
    post = QuestionPublicationRecoveryTest.post

    def setUp(self):
        QuestionPublicationRecoveryTest.setUp(self)
        self.lost = False
        self.req = {'pr': 7}
        self.assessment.update(decision='plan', questions=[], plan='Implement behavior.', acceptance=['Correct output'])
        p = patch.object(Workflow, 'feedback_context', return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.batch = {'id': 'fix', 'items': ['issue:12'], 'status': 'paused',
                      'review': {'complete': False, 'blocking': ['Which format?']}, 'tasks': [],
                      'commit_plan': {'units': []}}
        self.data = {'batches': [self.batch], 'feedback': [], 'worktree': str(self.root)}
        self.owner = SimpleNamespace(path=self.root / 'owner', execution_lock=self.root / 'owner.lock',
                                     load=lambda: self.data, save=Mock())
        for target, kwargs in [
                ('pr_review.issues.owner_fixes.execute', {'side_effect': lambda *a: self.data}),
                ('pr_review.issues.owner_git.validate', {}),
                ('pr_review.issues.owner_git.clean', {})]:
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def flow(self):
        flow = Workflow(self.req, self.repo, self.collect())
        flow.state['configuration'] = {}
        return flow

    def blocked_flow(self):
        flow = self.flow()
        self.assertFalse(flow.plan())
        original = flow.state['applied_assessment']
        with self.assertRaises(ApiError) as caught:
            flow.fix(self.owner, ['issue:12'])
        self.assertEqual('needs_attention', caught.exception.code)
        self.assertEqual(['Which format?'], self.flow().state['execution_blockers'])
        return original

    def test_unchanged_discussion_applies_blocker_questions_once_across_retries(self):
        original = self.blocked_flow()
        before = len(self.remote)
        self.assessment.update(decision='questions', questions=['Which format should implementation use?'])
        for _ in range(3):
            flow = self.flow()
            self.assertFalse(flow.plan())
            self.assertEqual('answers', flow.state['waiting'])
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(['Which format?'], self.assess.call_args.args[0]['execution_blockers'])
        self.assertNotEqual(original, flow.state['applied_assessment'])
        self.assertEqual(flow.state['input_digest'], flow.state['applied_assessment'])
        self.assertEqual(1, flow.state['question_round'])
        self.assertEqual(before + 1, len(self.remote))
        self.assertIn('**Q1.1** Which format should implementation use?', self.remote[-1]['body'])

    def test_successful_execution_clears_blockers_without_reapplying_assessment(self):
        self.blocked_flow()
        self.assessment['decision'] = 'continue'
        flow = self.flow()
        self.assertFalse(flow.plan())
        applied = flow.state['applied_assessment']
        self.batch.update(status='ready', review={'complete': True, 'blocking': []})
        flow.fix(self.owner, ['issue:12'])
        self.assertNotIn('execution_blockers', self.flow().state)
        self.assertFalse(self.flow().plan())
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(applied, self.flow().state['applied_assessment'])
        # A later occurrence of the same blocker is a new reassessment request.
        self.batch.update(status='paused', review={'complete': False, 'blocking': ['Which format?']})
        with self.assertRaises(ApiError):
            self.flow().fix(self.owner, ['issue:12'])
        self.assessment.update(decision='questions', questions=['Which format now?'])
        self.assertFalse(self.flow().plan())
        self.assertEqual(3, self.assess.call_count)
        self.assertEqual(1, self.flow().state['question_round'])
        self.assertNotEqual(applied, self.flow().state['applied_assessment'])

    def test_legacy_pending_blockers_apply_new_assessment(self):
        original = self.blocked_flow()
        flow = self.flow()
        flow.state.pop('reassessment_generation', None)
        flow.state.pop('input_digest', None)
        flow.save()
        self.assessment.update(decision='questions', questions=['Which format?'])
        self.assertFalse(self.flow().plan())
        saved = self.flow().state
        self.assertNotEqual(original, saved['applied_assessment'])
        self.assertEqual(1, saved['reassessment_generation'])
        self.assertEqual(1, saved['question_round'])

    def test_lost_question_response_resumes_same_blocker_assessment(self):
        self.blocked_flow()
        self.assessment.update(decision='questions', questions=['Which format?'])
        self.lost = True
        with self.assertRaises(ApiError) as caught:
            self.flow().plan()
        self.assertEqual('uncertain', caught.exception.code)
        posts = self.posts
        for _ in range(2):
            self.assertFalse(self.flow().plan())
        self.assertEqual(posts, self.posts)
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(1, self.flow().state['question_round'])

    def test_unverified_recovery_retains_blockers(self):
        self.blocked_flow()
        self.batch.update(status='ready', review={'complete': True, 'blocking': []},
                          commit_execution={'status': 'complete', 'inspection_required': True})
        with self.assertRaises(ApiError) as caught:
            self.flow().fix(self.owner, ['issue:12'])
        self.assertEqual('needs_attention', caught.exception.code)
        self.assertEqual(['Which format?'], self.flow().state['execution_blockers'])


class TerminalRecoveryTest(unittest.TestCase):
    """Persist and reload terminal decisions without Git or external writes."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = SimpleNamespace(identity='github.com/org/repo', name='org/repo',
                                    common=self.root, cwd=self.root,
                                    git=Mock(return_value=SimpleNamespace(stdout='head\n')))
        self.f = {**facts(), 'head': 'head'}
        self.req = {'command': 'issue-run', 'issue': 12}
        self.assessment = {'decision': 'no_change', 'summary': 'Already correct.',
                           'questions': [], 'plan': '', 'acceptance': []}
        self.remote = []
        for target, kwargs in [
                ('pr_review.issues.state_root', {'return_value': self.root}),
                ('pr_review.issues.resolve', {'return_value': self.repo}),
                ('pr_review.issues.collect_eligibility_facts', {'side_effect': self.collect}),
                ('pr_review.issues.assess_issue', {'side_effect': lambda *a: copy.deepcopy(self.assessment)}),
                ('pr_review.issues.Workflow.workspace', {'return_value': self.root}),
                ('pr_review.issues.Workflow.clean', {}),
                ('pr_review.issues.Workflow.bootstrap', {'autospec': True, 'side_effect': AssertionError('Terminal decision executed a plan')}),
                ('pr_review.issue_writes.pages', {'side_effect': lambda *a: copy.deepcopy(self.remote)}),
                ('pr_review.issue_writes.Writes.api', {'side_effect': self.post})]:
            p = patch(target, **kwargs)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith('assess_issue'):
                self.assess = mocked
            if target.endswith('Writes.api'):
                self.api = mocked

    def collect(self, *args):
        return {**copy.deepcopy(self.f), 'comments': copy.deepcopy(self.remote)}

    def post(self, endpoint, payload):
        self.assertTrue(endpoint.endswith('/issues/12/comments'))
        self.remote.append({'id': len(self.remote) + 1, 'body': payload['body'],
                            'user': {'login': 'author'}, 'created_at': '2026-10-30'})
        return self.remote[-1]

    def flow(self):
        return Workflow(self.req, self.repo, self.f)

    def assert_terminal_recovery(self, decision, phase, code):
        self.assessment['decision'] = decision
        first = dispatch(self.req)
        self.assertEqual(decision, first['outcome'])
        original = self.flow().state['applied_assessment']
        self.f['issue']['state'] = 'closed'
        for _ in range(2):
            failed = dispatch(self.req)
            self.assertEqual('needs_attention', failed['outcome'])
            self.assertEqual('preflight_failed', failed['error']['code'])
            self.assertEqual(phase, failed['phase'])
            self.assertEqual(phase, self.flow().state['phase'])
        self.f['issue']['state'] = 'open'
        for _ in range(2):
            result = dispatch({**self.req, 'yolo': True})
            self.assertEqual((decision, phase, code), (result['outcome'], result['phase'], result['exit_code']))
            self.assertNotIn('error', self.flow().state)
        self.assertEqual(original, self.flow().state['applied_assessment'])
        self.assertNotIn('plan', self.flow().state)
        self.assertEqual(1, self.assess.call_count)
        self.api.assert_called_once()

    def test_completed_no_change_survives_close_reopen(self):
        self.assert_terminal_recovery('no_change', 'complete', 0)

    def test_unsuitable_survives_close_reopen_and_changed_input_reassesses(self):
        self.assert_terminal_recovery('unsuitable', 'blocked', 3)
        self.f['issue']['body'] += ' New requirements.'
        self.assessment.update(decision='questions', questions=['Which behavior?'])
        result = dispatch(self.req)
        self.assertEqual(('answers', 'waiting', 2), (result['outcome'], result['phase'], result['exit_code']))
        self.assertEqual(2, self.assess.call_count)

    def test_initial_reconciliation_failure_keeps_completed_phase(self):
        self.assertEqual('no_change', dispatch(self.req)['outcome'])
        with patch.object(Writes, 'reconcile', side_effect=ApiError('network', 'Read failed')):
            result = dispatch(self.req)
        self.assertEqual('network', result['error']['code'])
        self.assertEqual('complete', self.flow().state['phase'])
        self.assertEqual('no_change', dispatch(self.req)['outcome'])
        self.api.assert_called_once()

    def test_legacy_blocked_no_change_without_plan_restores_completion(self):
        self.assertEqual('no_change', dispatch(self.req)['outcome'])
        flow = self.flow()
        flow.state.update(phase='blocked', error={'code': 'preflight_failed'})
        flow.save()
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual(('no_change', 'complete', 0), (result['outcome'], result['phase'], result['exit_code']))
        self.assertNotIn('plan', self.flow().state)
        self.assertEqual(1, self.assess.call_count)
        self.api.assert_called_once()

    def test_terminal_assessments_supersede_an_older_approved_plan_on_retry(self):
        self.assessment.update(decision='plan', plan='Old implementation.', acceptance=['Old behavior'])
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.remote.append({'id': 99, 'body': '/fix-issue approve 1',
                            'user': {'login': 'author'}, 'created_at': '2026-10-31'})
        for decision, phase, code in [('no_change', 'complete', 0), ('unsuitable', 'blocked', 3)]:
            with self.subTest(decision=decision):
                self.f['issue']['body'] += ' Changed scope.'
                self.assessment['decision'] = decision
                self.assertEqual(decision, dispatch(self.req)['outcome'])
                flow = self.flow()
                # State left by the former exception handler after a transient failure.
                flow.state.update(phase='blocked', error={'code': 'preflight_failed'})
                flow.save()
                posts = self.api.call_count
                for _ in range(2):
                    result = dispatch({**self.req, 'approve': 1, 'yolo': True})
                    self.assertEqual((decision, phase, code), (result['outcome'], result['phase'], result['exit_code']))
                self.assertEqual(posts, self.api.call_count)
                self.assertEqual(1, self.flow().state['plan']['revision'])
        self.assertEqual(3, self.assess.call_count)

    def check_replanning_after_execution_error(self, decision, *, reject_publication=False):
        self.assessment.update(decision='plan', plan='Old implementation.', acceptance=['Old behavior'])
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.remote.append({'id': 99, 'body': '/fix-issue approve 1',
                            'user': {'login': 'author'}, 'created_at': '2099-01-01'})

        def fail_execution(flow):
            flow.state['phase'] = self.execution_phase
            flow.save()
            raise ApiError('network', 'Transient execution failure')

        with patch.object(Workflow.bootstrap, 'side_effect', fail_execution):
            failed = dispatch(self.req)
            self.assertEqual('network', failed['error']['code'])
            self.assertEqual(self.execution_phase, self.flow().state['phase'])
            old_input = self.flow().state['applied_assessment']
            self.f['issue']['body'] += ' Changed requirements.'
            self.assessment.update(decision=decision, questions=['Which new behavior?'])
            if reject_publication:
                with patch.object(Writes, 'api', side_effect=ApiError('http', 'HTTP 403 Forbidden')):
                    failed = dispatch(self.req)
                self.assertEqual('publication_rejected', failed['error']['code'])
                self.assertEqual('intake', self.flow().state['phase'])
                self.assertEqual(old_input, self.flow().state['applied_assessment'])
            expected = {'questions': ('answers', 'waiting', 2),
                        'no_change': ('no_change', 'complete', 0),
                        'unsuitable': ('unsuitable', 'blocked', 3)}[decision]
            for _ in range(2):
                result = dispatch(self.req)
                self.assertEqual(expected, (result['outcome'], result['phase'], result['exit_code']))
                self.assertNotIn('error', self.flow().state)
            Workflow.bootstrap.assert_called_once()
        self.assertNotEqual(old_input, self.flow().state['applied_assessment'])
        self.assertEqual(2, self.assess.call_count)
        self.assertEqual(2, self.api.call_count)

    def test_approved_retry_can_publish_replacement_questions(self):
        self.execution_phase = 'approved'
        self.check_replanning_after_execution_error('questions')

    def test_working_retry_can_publish_replacement_no_change(self):
        self.execution_phase = 'working'
        self.check_replanning_after_execution_error('no_change')

    def test_approved_retry_can_publish_replacement_unsuitable(self):
        self.execution_phase = 'approved'
        self.check_replanning_after_execution_error('unsuitable')

    def test_replacement_publication_failure_resumes_pending_assessment(self):
        self.execution_phase = 'working'
        self.check_replanning_after_execution_error('questions', reject_publication=True)


class WriteRecoveryTest(unittest.TestCase):
    def make_writes(self, kind):
        self.remote = []
        self.state = {'id': 'workflow', 'viewer': 'author', 'issue': 12, 'actions': {},
                      'remote_branch': 'fix-issue/12', 'base_branch': 'main', 'title': 'Fix', 'plan': {'body': 'Plan', 'revision': 1}}
        self.save = Mock()
        self.writes = Writes(SimpleNamespace(name='org/repo', identity='github.com/org/repo'), self.state, self.save, Mock())
        if kind == 'pr':
            self.publish_plan_receipt()
        self.key = 'questions:digest' if kind == 'comment' else 'create-pr'
        self.send = (lambda: self.writes.comment('**Q1.1** Which behavior?', self.key)) if kind == 'comment' else self.writes.create_pr
        self.writes.api = Mock(side_effect=self.post)

    def publish_plan_receipt(self):
        body = '## Plan revision 1\n\n' + self.state['plan']['body']
        self.state['actions']['plan:1'] = {'status': 'complete', 'body': body,
            'receipt': {'id': 100, 'body': self.writes.comment_text(body, 'plan:1'),
                        'user': {'login': 'author'}}}

    def test_pr_body_links_plan_without_importing_closing_references(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for host in ('github.com', 'github.example.com'):
                with self.subTest(host=host):
                    self.make_writes('pr')
                    self.writes.repo.identity = host + '/org/repo'
                    self.state['plan']['body'] = ('This fixes #34, closes other/repo#56, and resolves '
                        'https://github.com/other/repo/issues/78.\n\nCloses #90')
                    self.publish_plan_receipt()
                    self.state['actions']['plan:1']['receipt']['html_url'] = 'https://evil.example/)\nFixes #99'
                    self.send()
                    payload = self.writes.api.call_args.args[1]
                    self.assertEqual(f'Closes #12\n\nPlan: https://{host}/org/repo/issues/12#issuecomment-100'
                                     '\n\n<!-- fix-issue workflow=workflow -->', payload['body'])
                    self.assertTrue(payload['draft'])
                    self.assertIn('fixes #34', self.state['plan']['body'])

    def test_pr_requires_a_valid_current_plan_receipt_before_sending(self):
        cases = [lambda a: a.clear(), lambda a: a.update(status='submitting'),
                 lambda a: a['receipt'].update(id='100)\nFixes #34'),
                 lambda a: a['receipt'].update(id=True), lambda a: a['receipt'].update(id=0),
                 lambda a: a['receipt'].update(body='Edited'),
                 lambda a: a['receipt'].update(user={'login': 'other'})]
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for change in cases:
                with self.subTest(change=change):
                    self.make_writes('pr')
                    change(self.state['actions']['plan:1'])
                    with self.assertRaises(ApiError) as caught:
                        self.send()
                    self.assertEqual('facts_incomplete', caught.exception.code)
                    self.writes.api.assert_not_called()
            self.make_writes('pr')
            self.state['plan']['revision'] = 2
            with self.assertRaises(ApiError) as caught:
                self.send()
            self.assertEqual('facts_incomplete', caught.exception.code)
            self.writes.api.assert_not_called()

    def test_absent_legacy_payload_is_replaced_with_plan_link_before_sending(self):
        self.make_writes('pr')
        previous = {'title': 'Fix', 'head': 'fix-issue/12', 'base': 'main', 'draft': True,
                    'body': 'Closes #12\n\nThis fixes #34\n\n<!-- fix-issue workflow=workflow -->'}
        self.state['actions']['create-pr'] = {'status': 'pending', 'payload': copy.deepcopy(previous)}
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            self.send()
        action = self.state['actions']['create-pr']
        self.assertNotIn('fixes #34', self.writes.api.call_args.args[1]['body'])
        self.assertIn('#issuecomment-100', action['payload']['body'])
        self.assertEqual(previous, action['history'][0]['previous_payload'])

    def test_uncertain_legacy_payload_is_preserved_and_never_resent(self):
        self.make_writes('pr')
        previous = {'status': 'submitting', 'payload': {'body':
            'Closes #12\n\nThis fixes #34\n\n<!-- fix-issue workflow=workflow -->'}}
        self.state['actions']['create-pr'] = copy.deepcopy(previous)
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            with self.assertRaises(ApiError) as caught:
                self.send()
        self.assertEqual('uncertain', caught.exception.code)
        self.assertEqual(previous, self.state['actions']['create-pr'])
        self.writes.api.assert_not_called()

    def test_accepted_legacy_payload_is_reconciled_without_rewriting_or_resending(self):
        self.make_writes('pr')
        payload = {'body': 'Closes #12\n\nThis fixes #34\n\n<!-- fix-issue workflow=workflow -->'}
        self.state['actions']['create-pr'] = {'status': 'submitting', 'payload': copy.deepcopy(payload)}
        receipt = self.post('/pulls', payload)
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            self.assertEqual(receipt, self.send())
        self.assertEqual(payload, self.state['actions']['create-pr']['payload'])
        self.assertEqual('complete', self.state['actions']['create-pr']['status'])
        self.writes.api.assert_not_called()

    def post(self, endpoint, payload):
        value = {'id': 101, 'body': payload['body'], 'user': {'login': 'author'}, 'created_at': '2026-10-30'}
        if endpoint.endswith('/pulls'):
            value.update(number=7, base={'ref': 'main'}, head={'ref': 'fix-issue/12', 'repo': {'full_name': 'org/repo'}})
        self.remote.append(value)
        return value

    def page(self, *args):
        return copy.deepcopy(self.remote)

    def uncertain(self, *, accepted=False):
        def lose(endpoint, payload):
            if accepted:
                self.post(endpoint, payload)
            raise ApiError('network', 'Lost response')
        self.writes.api.side_effect = lose
        with self.assertRaises(ApiError) as caught:
            self.send()
        self.assertEqual('uncertain', caught.exception.code)
        self.writes.api.reset_mock()
        self.writes.api.side_effect = self.post

    def assert_recovery_blocked(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ApiError) as caught:
            self.writes.recover(self.key)
        self.assertEqual('uncertain', caught.exception.code)
        self.assertEqual(before, self.state)
        self.writes.api.assert_not_called()

    def test_accepted_comment_and_pr_reconcile_without_resend(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                with self.subTest(kind=kind):
                    self.make_writes(kind)
                    self.uncertain(accepted=True)
                    self.writes.reconcile()
                    self.assertEqual('complete', self.state['actions'][self.key]['status'])
                    self.assertEqual(self.remote[0], self.state['actions'][self.key]['receipt'])
                    if kind == 'pr':
                        self.assertEqual(7, self.state['pr'])
                        self.assertTrue(self.state['managed'])
                    self.writes.api.assert_not_called()

    def test_absent_comment_and_pr_retry_only_after_explicit_decision(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                with self.subTest(kind=kind):
                    self.make_writes(kind)
                    self.uncertain()
                    for _ in range(2):
                        with self.assertRaises(ApiError):
                            self.send()
                    previous = copy.deepcopy(self.state['actions'][self.key])
                    self.assertEqual('absent', self.writes.recover()[self.key]['readback']['outcome'])
                    self.assertEqual(previous, self.state['actions'][self.key])
                    self.writes.recover(self.key)
                    action = self.state['actions'][self.key]
                    self.assertEqual('pending', action['status'])
                    self.assertEqual(previous, action['history'][0]['previous'])
                    self.assertEqual('author', action['history'][0]['viewer'])
                    self.writes.api.assert_not_called()
                    self.send()
                    self.writes.api.assert_called_once()
                    self.assertEqual('complete', action['status'])
                    self.assertEqual(1, len(action['history']))

    def test_absence_decision_fresh_read_finds_accepted_write(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                with self.subTest(kind=kind):
                    self.make_writes(kind)
                    self.uncertain()
                    self.assertEqual('absent', self.writes.recover()[self.key]['readback']['outcome'])
                    payload = {'body': self.writes.comment_text(self.state['actions'][self.key]['body'], self.key)} if kind == 'comment' else self.state['actions'][self.key]['payload']
                    self.post('/comments' if kind == 'comment' else '/pulls', payload)
                    self.writes.recover(self.key)
                    self.assertEqual('complete', self.state['actions'][self.key]['status'])
                    self.assertNotIn('history', self.state['actions'][self.key])
                    self.writes.api.assert_not_called()

    def test_ambiguous_changed_deleted_and_foreign_writes_block_reset(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                for change in ('duplicate', 'edited', 'deleted', 'foreign', 'edited_receipt'):
                    with self.subTest(kind=kind, change=change):
                        self.make_writes(kind)
                        self.uncertain(accepted=True)
                        if change in ('deleted', 'edited_receipt'):
                            self.writes.reconcile()
                        if change == 'duplicate':
                            self.remote.append({**self.remote[0], 'id': 102})
                        elif change == 'edited':
                            self.remote[0]['body'] = 'Edited wording\n\n' + self.remote[0]['body'].split('\n\n')[-1]
                        elif change == 'deleted':
                            self.remote.clear()
                        elif change == 'foreign':
                            self.remote[0]['user'] = {'login': 'someone-else'}
                        else:
                            self.remote[0]['body'] = 'Removed all markers'
                        self.assert_recovery_blocked()
                        with self.assertRaises(ApiError):
                            self.send()
                        self.writes.api.assert_not_called()

    def test_failed_readback_preserves_intent_and_blocks_reset(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                with self.subTest(kind=kind):
                    self.make_writes(kind)
                    self.uncertain()
                    before = copy.deepcopy(self.state)
                    with patch('pr_review.issue_writes.pages', side_effect=ApiError('facts_incomplete', 'Read failed')):
                        for operation in (self.writes.reconcile, self.writes.recover, lambda: self.writes.recover(self.key)):
                            with self.assertRaises(ApiError):
                                operation()
                    self.assertEqual(before, self.state)
                    self.writes.api.assert_not_called()

    def test_legacy_uncertain_pr_without_payload_recovers(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            self.make_writes('pr')
            self.uncertain(accepted=True)
            self.state['actions'][self.key].pop('payload')
            self.writes.reconcile()
            self.assertEqual(7, self.state['pr'])
            self.assertEqual('complete', self.state['actions'][self.key]['status'])
            self.writes.api.assert_not_called()

    def test_pending_intent_is_read_back_before_assessment_too(self):
        # A rejected read-back after an accepted POST can leave legacy actions
        # pending, so status alone cannot prove that the publication is absent.
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            for kind in ('comment', 'pr'):
                with self.subTest(kind=kind):
                    self.make_writes(kind)
                    self.uncertain(accepted=True)
                    self.state['actions'][self.key]['status'] = 'pending'
                    accepted = self.remote.pop()
                    self.writes.reconcile()
                    self.assertEqual('pending', self.state['actions'][self.key]['status'])
                    self.remote.append(accepted)
                    self.writes.reconcile()
                    self.assertEqual('complete', self.state['actions'][self.key]['status'])
                    self.writes.api.assert_not_called()

    def test_recovery_is_action_specific_and_retains_prior_decisions(self):
        with patch('pr_review.issue_writes.pages', side_effect=self.page):
            self.make_writes('comment')
            self.uncertain()
            other = {'status': 'submitting', 'body': 'Different question'}
            self.state['actions']['questions:other'] = copy.deepcopy(other)
            for _ in range(2):
                self.writes.recover(self.key)
                self.uncertain()
            action = self.state['actions'][self.key]
            self.assertEqual(2, len(action['history']))
            self.assertEqual(other, self.state['actions']['questions:other'])
            self.writes.api.assert_not_called()


class WriteRecoveryCliTest(unittest.TestCase):
    def invoke(self, argv, **response):
        from pr_review.issue_cli import main
        output = StringIO()
        with patch('pr_review.issue_cli.dispatch', **response) as dispatched, \
                patch('pr_review.issue_cli.local_sessions', return_value=nullcontext()), \
                patch('pr_review.issue_cli.cancellation_scope', return_value=nullcontext()), \
                patch('pr_review.issue_cli.event_sink', return_value=nullcontext()), \
                patch('pr_review.issue_cli.os.umask'), redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue(), dispatched

    def test_inspection_and_retry_route_to_recovery_and_print_journal(self):
        for options, key in [(['--inspect-writes'], None), (['--retry-absent', 'plan:1'], 'plan:1')]:
            with self.subTest(options=options):
                result = {'outcome': 'inspected', 'exit_code': 0, 'actions': {'plan:1': {'status': 'submitting'}}}
                code, output, dispatched = self.invoke(['-i', '12', *options, '--json'], return_value=result)
                self.assertEqual(0, code)
                self.assertEqual(result, json.loads(output))
                request = dispatched.call_args.args[0]
                self.assertEqual('issue-recover', request['command'])
                self.assertEqual(key, request['retry_absent'])
        _, output, _ = self.invoke(['-i', '12', '--inspect-writes'], return_value=result)
        self.assertIn('plan:1', output)

    def test_recovery_failure_retains_uncertain_exit_code(self):
        code, output, _ = self.invoke(['-i', '12', '--retry-absent', 'plan:1', '--json'],
                                     side_effect=ApiError('uncertain', 'Multiple matches', exit_code=5))
        self.assertEqual(5, code)
        self.assertEqual('uncertain', json.loads(output)['reason_code'])

    def test_shared_dispatch_advertises_and_routes_recovery(self):
        from pr_review.application import dispatch as application_dispatch
        self.assertIn('issue-recover', application_dispatch({'command': 'capabilities'})['commands'])
        request = {'command': 'issue-recover', 'issue': 12, 'retry_absent': 'plan:1'}
        with patch('pr_review.issues.dispatch', return_value={'outcome': 'recovered'}) as dispatched:
            self.assertEqual({'outcome': 'recovered'}, application_dispatch(request))
        dispatched.assert_called_once_with(request)


class IssueIntegrationTest(unittest.TestCase):
    # Reuse the existing real-Git fixture without inheriting its test cases.
    git = test_owner.OwnerTest.git
    github = test_owner.OwnerTest.github
    model = test_owner.OwnerTest.model
    publish = test_owner.OwnerTest.publish
    request = test_owner.OwnerTest.request
    item = staticmethod(test_owner.OwnerTest.item)

    def setUp(self):
        test_owner.OwnerTest.setUp(self)
        self.feedback.clear()
        self.git('branch', 'remote-feature', self.head)
        self.f = facts()
        self.f.update(base=self.base, head=self.head, pr={'number': 7, 'state': 'open', 'assignees': [], 'draft': True,
                     'head': {'ref': 'remote-feature', 'sha': self.head, 'repo': {'full_name': 'org/repo'}},
                     'base': {'ref': 'main'}, 'user': {'login': 'author'}})
        self.metadata['headRefName'] = 'remote-feature'
        self.f['pulls'] = [self.f['pr']]
        self.assessments = []
        self.assessment = {'decision': 'plan', 'summary': 'Plan', 'plan': 'Implement the fix and test it.', 'acceptance': ['Correct behavior'], 'questions': []}
        self.repo_object = owner_state.OwnerSession({'session': self.path}).repo
        self.req = {'command': 'issue-run', 'cwd': str(self.repo), 'issue': 12, 'reviewers': ['codex']}
        for target, effect in [('pr_review.issues.collect_eligibility_facts', self.collect),
                               ('pr_review.issues.assess_issue', self.assess),
                               ('pr_review.issue_writes.pages', self.page),
                               ('pr_review.issues.pages', self.page),
                               ('pr_review.issue_writes.Writes.api', self.api)]:
            p = patch(target, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)
        self.reviews = patch.object(Workflow, 'review', return_value=[])
        self.reviews.start()
        self.addCleanup(self.reviews.stop)

    def collect(self, target, repo):
        sha = self.git('rev-parse', 'remote-feature')
        self.metadata['headRefOid'] = sha
        self.git('update-ref', 'refs/pull/7/head', sha)
        self.f['head'] = sha
        self.f['pr']['head']['sha'] = sha
        return copy.deepcopy(self.f)

    def assess(self, *args):
        self.assessments.append(args)
        return copy.deepcopy(self.assessment)

    def page(self, repo, endpoint):
        return copy.deepcopy(self.f['comments']) if '/issues/12/comments' in endpoint else copy.deepcopy(self.feedback) if '/pulls/7/comments' in endpoint else []

    def api(self, endpoint, payload, method='POST'):
        if endpoint == 'graphql':
            if payload['query'].startswith('query'):
                return {'data': {'repository': {'pullRequest': {'id': 'P7', 'state': 'OPEN', 'isDraft': self.f['pr']['draft']}}}}
            desired = 'convertPullRequestToDraft' in payload['query']
            self.f['pr']['draft'] = desired
            verb = 'convertPullRequestToDraft' if desired else 'markPullRequestReadyForReview'
            return {'data': {verb: {'pullRequest': {'isDraft': desired}}}}
        self.assertIn('/issues/12/comments', endpoint)
        value = {'id': len(self.f['comments']) + 1, 'body': payload['body'], 'user': {'login': 'author'}, 'created_at': '2026-10-30T23:59:59+00:00'}
        self.f['comments'].append(value)
        return value

    def test_plan_cli_approval_real_owner_commit_push_and_noop(self):
        # Dirty main checkout must survive the entire implementation.
        (self.repo / 'source.txt').write_text('manual main checkout edit\n')
        first = dispatch(self.req)
        self.assertEqual(2, first['exit_code'], first)
        self.assertEqual(1, len(self.assessments))
        again = dispatch(self.req)
        self.assertEqual(2, again['exit_code'], again)
        self.assertEqual(1, len(self.assessments))
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual('ready', result['outcome'])
        self.assertEqual('manual main checkout edit\n', (self.repo / 'source.txt').read_text())
        owner = owner_state.OwnerSession({'session': self.path})
        data = owner.load()
        self.assertTrue(any(i['kind'] == 'issue_plan' and not i.get('missing') for i in data['feedback']))
        self.assertEqual([], data['replies'])
        self.assertTrue(data['batches'][0]['commit_progress']['complete'])
        self.assertEqual(self.git('rev-parse', 'remote-feature'), self.git('rev-parse', 'HEAD', cwd=data['worktree']))
        result = dispatch(self.req)
        self.assertEqual('noop', result['outcome'], result)

    def test_comment_metadata_does_not_repeat_owner_implementation(self):
        request = {**self.req, 'issue': None, 'pr': 7, 'yolo': True}
        comment = {'id': 100, 'user': {'login': 'human', 'avatar_url': 'old'},
                   'body': 'Retain history.', 'reactions': {'total_count': 0}}
        self.f['comments'].append(comment)
        with patch.object(Writes, 'push', autospec=True, side_effect=self.simulate_push):
            result = dispatch(request)
            self.assertEqual('ready', result['outcome'], result)
            owner = owner_state.OwnerSession({'session': self.path})
            original = copy.deepcopy(next(i for i in owner.load()['feedback'] if i['kind'] == 'issue_plan'))
            calls = len(self.calls)
            comment['reactions']['total_count'] = 1
            comment['user']['avatar_url'] = 'new'
            self.assertEqual('noop', dispatch(request)['outcome'])
            # Reenter bootstrap and import instead of taking the readiness cache.
            flow = Workflow(request, self.repo_object, self.f)
            flow.state.pop('ready_base')
            flow.save()
            result = dispatch(request)
            self.assertEqual('ready', result['outcome'], result)
            current = next(i for i in owner.load()['feedback'] if i['kind'] == 'issue_plan')
            self.assertEqual(original['fingerprint'], current['fingerprint'])
            self.assertEqual(calls, len(self.calls))
            self.assertEqual(1, len(owner.load()['batches']))
            self.assertEqual(1, len(self.assessments))

    def test_verified_noncommitting_review_does_not_repeat_on_active_retries(self):
        request = {**self.req, 'issue': None, 'pr': 7, 'yolo': True}
        with patch.object(Writes, 'push', autospec=True, side_effect=self.simulate_push):
            self.assertEqual('ready', dispatch(request)['outcome'])
            self.head = self.git('rev-parse', 'remote-feature')
            review = test_owner.OwnerTest.saved_review(self)
            data = review.load()
            data['mutation_tracking'] = True
            review.save(data)
            self.reviews.stop()

            def no_change(session, state, batch, role, context):
                if role == 'plan':
                    return self.model(session, state, batch, role, context)
                self.calls.append((role, context))
                return {'summary': 'Already satisfied', 'outcomes': [
                    {'item': item['key'], 'status': 'already_addressed' if n == 0 else 'no_change',
                     'evidence': 'Traced existing behavior'} for n, item in enumerate(context['feedback'])],
                    'complete': True, 'checks': ['code tracing'], 'blocking': []}

            with patch('pr_review.reviews.execute', return_value=review), \
                    patch('pr_review.owner_providers.invoke', side_effect=no_change):
                flow = Workflow(request, self.repo_object, self.f)
                flow.state.pop('ready_base')
                flow.save()
                result = dispatch(request)
                self.assertEqual('ready', result['outcome'], result)
                owner = owner_state.OwnerSession({'session': self.path})
                saved = owner.load()
                locals_ = [i for i in saved['feedback'] if i['kind'] == 'local_review']
                self.assertEqual(2, len(locals_))
                self.assertTrue(all(not i['attention'] and i.get('local_completion') for i in locals_))
                self.assertEqual([], saved['replies'])
                self.assertEqual(2, len(saved['batches']))
                calls = len(self.calls)
                # A retry also repairs retained attention before selection.
                for item in saved['feedback']:
                    if item['kind'] == 'local_review':
                        item.pop('local_completion')
                        item.update(attention=True, queued=True, triage='attention')
                owner.save(saved)
                # Bypass the readiness cache to exercise selection and reimport
                # in another active invocation against the identical review.
                flow = Workflow(request, self.repo_object, self.f)
                flow.state.pop('ready_base')
                flow.save()
                self.assertEqual('ready', dispatch(request)['outcome'])
                self.assertEqual(calls, len(self.calls))
                self.assertEqual(2, len(owner.load()['batches']))
                self.assertEqual('noop', dispatch(request)['outcome'])

    def test_post_commit_hook_edits_block_push_and_readiness_on_retries(self):
        hook = self.repo / '.git' / 'hooks' / 'post-commit'
        hook.write_text("#!/bin/sh\nprintf 'unverified hook edit\\n' >> source.txt\n")
        hook.chmod(0o755)
        with patch.object(Writes, 'push') as push, \
                patch('pr_review.issues.owner_publication.publish') as publish:
            for _ in range(2):
                result = dispatch({**self.req, 'yolo': True})
                self.assertEqual('needs_attention', result['outcome'], result)
                self.assertEqual(3, result['exit_code'])
                data = owner_state.OwnerSession({'session': self.path}).load()
                batch = data['batches'][-1]
                self.assertTrue(batch['commit_progress']['complete'])
                self.assertTrue(batch['commit_execution']['inspection_required'])
                self.assertTrue(self.git('status', '--porcelain', cwd=data['worktree']))
                self.assertIn('unverified hook edit', (Path(data['worktree']) / 'source.txt').read_text())
                self.assertTrue(self.f['pr']['draft'])
                self.assertFalse(any(k.startswith('ready:') for k in Workflow(self.req, self.repo_object, self.f).state['actions']))
            push.assert_not_called()
            publish.assert_not_called()

    def check_unverified_edits_block_push(self, *, staged):
        from pr_review import owner_fixes
        execute = owner_fixes.execute
        def execute_then_edit(*args):
            view = execute(*args)
            path = Path(view['worktree']) / 'source.txt'
            path.write_text('unverified later edit\n')
            if staged:
                self.git('add', 'source.txt', cwd=view['worktree'])
                path.write_text('fixed\n')
            return view

        with patch('pr_review.issues.owner_fixes.execute', side_effect=execute_then_edit), \
                patch.object(Writes, 'push') as push:
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual('needs_attention', result['outcome'], result)
        self.assertEqual('dirty_worktree', result['error']['code'])
        self.assertTrue(self.f['pr']['draft'])
        push.assert_not_called()

    def test_unverified_source_after_execution_blocks_push(self):
        self.check_unverified_edits_block_push(staged=False)

    def test_unverified_index_after_execution_blocks_push(self):
        self.check_unverified_edits_block_push(staged=True)

    def simulate_push(self, writes, sha, expected, worktree):
        # Model a receipted push without invoking git push or a remote service.
        self.git('update-ref', 'remote-feature', sha)
        writes.state['remote_head'] = sha
        writes.save()

    def check_pending_push_retry(self, *, accepted):
        sends = []
        def transport(argv, **kwargs):
            self.assertEqual(['git', 'push'], argv[:2])
            sends.append(argv)
            sha = argv[3].split(':', 1)[0]
            if accepted or len(sends) > 1:
                self.git('update-ref', 'refs/heads/remote-feature', sha)
            if accepted:
                raise ApiError('network', 'Lost push response before confirmation')
            return SimpleNamespace(returncode=1 if len(sends) == 1 else 0, stderr='Rejected push')

        with patch('pr_review.issue_writes.command', side_effect=transport), \
                patch.object(Workflow, 'feedback_context', return_value=[]), \
                patch.object(Workflow, 'review', return_value=[]) as review:
            first = dispatch({**self.req, 'yolo': True})
            self.assertEqual('needs_attention', first['outcome'], first)
            self.assertTrue(self.f['pr']['draft'])
            review.assert_not_called()
            owner = owner_state.OwnerSession({'session': self.path})
            data = owner.load()
            head = self.git('rev-parse', 'HEAD', cwd=data['worktree'])
            self.assertNotEqual(self.head, head)
            self.assertFalse(any(i.get('attention') for i in data['feedback']))
            calls = len(self.calls)
            self.assertEqual(1, len(data['batches']))
            second = dispatch({**self.req, 'yolo': True})
            self.assertEqual('ready', second['outcome'], second)
            self.assertEqual(head, self.git('rev-parse', 'remote-feature'))
            saved = Workflow(self.req, self.repo_object, self.f).state
            self.assertEqual(head, saved['remote_head'])
            self.assertEqual(head, saved['ready_head'])
            self.assertEqual('complete', saved['actions']['push:' + head]['status'])
            self.assertEqual(1, saved['plan']['revision'])
            self.assertEqual(1, len(self.assessments))
            self.assertEqual(calls, len(self.calls))
            self.assertEqual(1, len(owner.load()['batches']))
            review.assert_called_once()
            self.assertEqual('noop', dispatch({**self.req, 'yolo': True})['outcome'])
        self.assertEqual(1 if accepted else 2, len(sends))

    def test_rejected_push_retries_without_selected_feedback(self):
        self.check_pending_push_retry(accepted=False)

    def test_accepted_unconfirmed_push_recovers_without_selected_feedback(self):
        self.check_pending_push_retry(accepted=True)

    def test_remote_head_moves_during_review_blocks_readiness(self):
        def review_then_move_head():
            # A review of the new implementation cannot authorize readiness
            # after the remote branch moves back to the original head.
            self.git('update-ref', 'remote-feature', self.head)
            return []

        with patch.object(Writes, 'push', autospec=True, side_effect=self.simulate_push), \
                patch.object(Workflow, 'review', side_effect=review_then_move_head):
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual('needs_attention', result['outcome'], result)
        self.assertIn(result['error']['code'], ('stale_head', 'preflight_failed'))
        self.assertTrue(self.f['pr']['draft'])
        self.assertFalse(any(k.startswith('ready:') for k in Workflow(self.req, self.repo_object, self.f).state['actions']))

    def test_unconfirmed_remote_head_blocks_review(self):
        def unconfirmed_push(writes, sha, expected, worktree):
            writes.state['remote_head'] = sha
            writes.save()

        with patch.object(Writes, 'push', autospec=True, side_effect=unconfirmed_push), \
                patch.object(Workflow, 'review', return_value=[]) as review:
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual('needs_attention', result['outcome'], result)
        self.assertIn(result['error']['code'], ('stale_head', 'preflight_failed'))
        self.assertTrue(self.f['pr']['draft'])
        review.assert_not_called()

    def test_source_changes_during_review_block_publication_and_readiness(self):
        def review_then_edit():
            owner = owner_state.OwnerSession({'session': self.path})
            data = owner.load()
            (Path(data['worktree']) / 'source.txt').write_text('edit during review\n')
            data['replies'].append({'status': 'staged', 'automatic': True})
            owner.save(data)
            return []

        with patch.object(Writes, 'push', autospec=True, side_effect=self.simulate_push), \
                patch.object(Workflow, 'review', side_effect=review_then_edit), \
                patch('pr_review.issues.owner_publication.prepare', return_value={'token': 'token'}) as prepare, \
                patch('pr_review.issues.owner_publication.publish') as publish:
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual('needs_attention', result['outcome'], result)
        self.assertEqual('dirty_worktree', result['error']['code'])
        self.assertTrue(self.f['pr']['draft'])
        prepare.assert_not_called()
        publish.assert_not_called()

    def test_cached_readiness_honors_retained_inspection_blocker(self):
        with patch.object(Writes, 'push', autospec=True, side_effect=self.simulate_push):
            self.assertEqual('ready', dispatch({**self.req, 'yolo': True})['outcome'])
            self.assertEqual('noop', dispatch({**self.req, 'yolo': True})['outcome'])
        owner = owner_state.OwnerSession({'session': self.path})
        data = owner.load()
        data['batches'][-1]['commit_execution']['inspection_required'] = True
        owner.save(data)
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual('needs_attention', result['outcome'], result)
        self.assertEqual('needs_attention', result['error']['code'])

    def test_yolo_questions_still_block_and_unchanged_is_noop(self):
        self.assessment.update(decision='questions', questions=['Which behavior is intended?'])
        first = dispatch({**self.req, 'yolo': True})
        self.assertEqual(2, first['exit_code'], first)
        self.assertEqual('answers', first['outcome'])
        count = len(self.f['comments'])
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(2, result['exit_code'])
        self.assertEqual(count, len(self.f['comments']))
        self.assertEqual(1, len(self.assessments))
        eligibility = dispatch({**self.req, 'command': 'issue-eligibility'})
        self.assertEqual('waiting_for_input', eligibility['disposition'])

    def test_unexpected_remote_commit_blocks_even_force_yolo(self):
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        old = self.git('rev-parse', 'remote-feature')
        sha = self.git('commit-tree', old + '^{tree}', '-p', old, '-m', 'human commit')
        self.git('update-ref', 'remote-feature', sha)
        result = dispatch({**self.req, 'force': True, 'yolo': True})
        self.assertEqual(3, result['exit_code'])
        self.assertIn('unexpected_head', str(result))

    def test_comment_uncertainty_reconciles_without_duplicate(self):
        f = self.collect({}, self.repo_object)
        flow = Workflow(self.req, self.repo_object, f)
        flow.eligibility = evaluate_eligibility(f)
        real = flow.writes.api
        def lost_response(*args, **kwargs):
            real(*args, **kwargs)
            raise ApiError('network', 'lost response')
        with patch.object(flow.writes, 'api', side_effect=lost_response):
            with self.assertRaises(ApiError):
                flow.writes.comment('Question', 'q1')
        count = len(self.f['comments'])
        receipt = flow.writes.comment('Question', 'q1')
        self.assertEqual(count, len(self.f['comments']))
        self.assertEqual('complete', flow.state['actions']['q1']['status'])
        self.assertEqual(count, receipt['id'])

    def test_stale_and_foreign_approval_do_not_execute(self):
        dispatch(self.req)
        self.f['comments'].append({'id': 50, 'body': '/fix-issue approve 1', 'user': {'login': 'other'}, 'created_at': '2026-10-30T23:59:59+00:00'})
        self.assessment['decision'] = 'continue'
        result = dispatch(self.req)
        self.assertEqual(2, result['exit_code'])
        self.assertEqual([], self.calls)
        result = dispatch({**self.req, 'approve': 2})
        self.assertEqual(3, result['exit_code'])

    def test_approval_revoked_before_push_blocks(self):
        dispatch(self.req)
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual(0, result['exit_code'], result)
        self.f['comments'] = [c for c in self.f['comments'] if not c['body'].startswith('/fix-issue approve ')]
        self.assessment['decision'] = 'continue'
        result = dispatch(self.req)
        self.assertEqual(2, result['exit_code'], result)

    def test_managed_workspace_edits_require_adoption(self):
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        owner = owner_state.OwnerSession({'session': self.path})
        worktree = Path(owner.load()['worktree'])
        (worktree / 'source.txt').write_text('unexpected edit\n')
        # A new issue response makes this an active invocation rather than ready no-op.
        self.f['comments'].append({'id': 99, 'body': 'Please check again', 'user': {'login': 'author'}, 'created_at': '2026-10-31'})
        self.assessment['decision'] = 'continue'
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(3, result['exit_code'], result)
        self.assertEqual('unexpected edit\n', (worktree / 'source.txt').read_text())

    def test_resume_from_linked_worktree_uses_same_issue_state(self):
        first = dispatch(self.req)
        linked = self.root / 'linked'
        self.git('worktree', 'add', '--detach', str(linked), self.head)
        second = dispatch({**self.req, 'cwd': str(linked)})
        self.assertEqual(first['session'], second['session'])
        self.assertEqual(1, len(self.assessments))

    def test_interrupted_fix_requires_adopt_then_reverification(self):
        real = self.model
        def interrupted(session, state, batch, role, context):
            result = real(session, state, batch, role, context)
            if role == 'fix':
                raise ApiError('model_failed', 'Interrupted after partial source edits')
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=interrupted):
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(3, result['exit_code'], result)
        owner = owner_state.OwnerSession({'session': self.path})
        worktree = owner.load()['worktree']
        self.assertEqual('fixed\n', (Path(worktree) / 'source.txt').read_text())
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(3, result['exit_code'], result)
        self.assertIn('interrupted_task', str(result))
        result = dispatch({**self.req, 'yolo': True, 'adopt': True})
        self.assertEqual(0, result['exit_code'], result)

    def test_new_plan_revision_updates_same_owner_task_with_history(self):
        dispatch({**self.req, 'yolo': True})
        self.f['comments'].append({'id': 999, 'body': 'Also handle the second case.', 'user': {'login': 'author'}, 'created_at': '2026-10-31'})
        self.assessment['plan'] = 'Implement the additional case and tests.'
        result = dispatch(self.req)
        self.assertEqual(2, result['exit_code'], result)
        result = dispatch({**self.req, 'approve': 2})
        self.assertEqual(0, result['exit_code'], result)
        data = owner_state.OwnerSession({'session': self.path}).load()
        items = [i for i in data['feedback'] if i['kind'] == 'issue_plan']
        self.assertEqual(1, len(items))
        self.assertEqual(2, items[0]['source']['plan']['revision'])
        self.assertTrue(items[0]['versions'])

    def test_verified_no_change_leaves_pr_draft(self):
        real = self.model
        def no_change(session, state, batch, role, context):
            if role == 'plan':
                return real(session, state, batch, role, context)
            return {'summary': 'Existing code already satisfies the acceptance criteria',
                    'outcomes': [{'item': i['key'], 'status': 'already_addressed', 'evidence': 'Verified existing code'} for i in context['feedback']],
                    'complete': True, 'checks': ['Focused verification'], 'blocking': []}
        with patch('pr_review.owner_providers.invoke', side_effect=no_change):
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual('no_change', result['outcome'])
        self.assertTrue(self.f['pr']['draft'])
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual('complete', result['outcome'])

    def test_answers_advance_to_plan_and_cli_approval(self):
        self.assessment.update(decision='questions', questions=['What should the output be?'])
        self.assertEqual(2, dispatch(self.req)['exit_code'])
        self.f['comments'].append({'id': 90, 'body': 'Q1.1: The output should be fixed.', 'user': {'login': 'author'}, 'created_at': '2026-10-30'})
        self.assessment.update(decision='plan', questions=[])
        self.assertEqual(2, dispatch(self.req)['exit_code'])
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(2, len(self.assessments))

    def test_later_review_feedback_uses_owner_and_publishes_verified_reply(self):
        self.assertEqual(0, dispatch({**self.req, 'yolo': True})['exit_code'])
        self.feedback.append(self.item('thread:T3', 103))
        self.assessment['decision'] = 'continue'
        real = self.model
        def followup(session, state, batch, role, context):
            result = real(session, state, batch, role, context)
            if role == 'fix':
                (Path(state['worktree']) / 'followup.txt').write_text('review correction\n')
                from pr_review import owner_git
                batch['checkpoint'] = owner_git.checkpoint(session, state, 'follow-up fixture')
            return result
        with patch('pr_review.owner_providers.invoke', side_effect=followup):
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(1, len(self.posts))
        followup_context = next(context for role, context in reversed(self.calls) if role == 'fix')
        self.assertIn('approved_issue_plan', followup_context)
        self.assertIn('Fixed in', self.posts[0][2]['body'])
        data = owner_state.OwnerSession({'session': self.path}).load()
        self.assertEqual('published', data['replies'][0]['status'])

    def test_changed_base_refreshes_plan_and_requires_current_revision(self):
        self.assertEqual(2, dispatch(self.req)['exit_code'])
        self.f['base'] = self.head
        self.assessment['decision'] = 'continue'
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual(3, result['exit_code'], result)
        self.assertIn('stale_approval', str(result))
        state = json.loads((Path(result['session']) / 'session.json').read_text())
        self.assertEqual(2, state['plan']['revision'])


class ReplacementPlanTest(unittest.TestCase):
    def setUp(self):
        self.t = IssueIntegrationTest()
        self.addCleanup(self.t.doCleanups)
        self.t.setUp()
        self.req = {**self.t.req, 'issue': None, 'pr': 7, 'yolo': True}
        p = patch.object(Writes, 'push', autospec=True, side_effect=self.t.simulate_push)
        self.push = p.start()
        self.addCleanup(p.stop)
        self.reviewed = []
        self.t.reviews.stop()
        p = patch.object(Workflow, 'review', autospec=True, side_effect=lambda flow:
                        self.reviewed.append((flow.state['assessed_base'], flow.state['remote_head'])) or [])
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual('ready', dispatch(self.req)['outcome'])
        self.owner = owner_state.OwnerSession({'session': self.t.path})
        self.original = copy.deepcopy(self.owner.load()['batches'][0])

    def assert_replacement_ready(self):
        result = dispatch(self.req)
        self.assertEqual('ready', result['outcome'], result)
        data = self.owner.load()
        self.assertEqual(2, len(data['batches']))
        old, new = data['batches']
        self.assertEqual(self.original['id'], old['id'])
        self.assertEqual(self.original['attempts'], old['attempts'])
        self.assertEqual(self.original['commit_execution'], old['commit_execution'])
        self.assertEqual(self.original['commit_progress'], old['commit_progress'])
        self.assertTrue(old['discussion_stale'])
        self.assertTrue(execution_complete(new))
        self.assertEqual(self.original['commit_progress']['commits'][-1], new['baseline'])
        item = next(i for i in data['feedback'] if i['kind'] == 'issue_plan')
        self.assertEqual(item['fingerprint'], new['verified_fingerprints'][item['key']])
        self.assertEqual(2, item['source']['plan']['revision'])
        self.assertEqual((self.t.f['base'], self.t.f['head']), self.reviewed[-1])
        self.assertEqual(2, len(self.reviewed))
        calls = len(self.t.calls)
        self.assertEqual('noop', dispatch({k: v for k, v in self.req.items() if k != 'approve'})['outcome'])
        self.assertEqual(calls, len(self.t.calls))
        return new

    def test_base_change_after_commits_starts_fresh_verification(self):
        self.t.f['base'] = self.t.head
        self.t.metadata['baseRefOid'] = self.t.head
        self.t.assessment['decision'] = 'continue'
        # A new base requires approval of the replacement revision, even when
        # inference continues the previous plan text.
        request = {**self.req, 'yolo': False}
        self.assertEqual('approval', dispatch(request)['outcome'])
        self.assertEqual(1, len(self.owner.load()['batches']))
        stale = dispatch({**request, 'approve': 1})
        self.assertEqual('stale_approval', stale['error']['code'])
        self.req = {**request, 'approve': 2}
        new = self.assert_replacement_ready()
        self.assertEqual([], new['commit_plan']['units'])
        self.assertEqual(self.t.head, new['verified_base'])
        state = Workflow(self.req, self.t.repo_object, self.t.f).state
        self.assertEqual(self.t.head, state['ready_base'])

    def test_new_requirement_after_commits_starts_fresh_implementation(self):
        self.t.f['comments'].append({'id': 999, 'body': 'Also retain history.',
                                    'user': {'login': 'author'}, 'created_at': '2026-10-31'})
        self.t.assessment.update(plan='Retain history in the second case.', acceptance=['History is retained'])
        real = self.t.model

        def implement(session, state, batch, role, context):
            result = real(session, state, batch, role, context)
            if role == 'fix':
                self.assertEqual(2, context['approved_issue_plan']['plan']['revision'])
                (Path(state['worktree']) / 'history.txt').write_text('retained\n')
                batch['checkpoint'] = test_owner.owner_git.checkpoint(session, state, 'replacement implementation')
            return result

        with patch('pr_review.owner_providers.invoke', side_effect=implement):
            new = self.assert_replacement_ready()
        self.assertTrue(new['commit_progress']['commits'])
        self.assertEqual('retained\n', (Path(self.owner.load()['worktree']) / 'history.txt').read_text())

    def test_incomplete_replacement_blocks_publication_and_resumes_itself(self):
        self.t.f['issue']['body'] += ' Also retain history.'
        self.t.assessment['plan'] = 'Retain history.'
        calls = self.push.call_count
        real = self.t.model

        def incomplete(session, state, batch, role, context):
            if role == 'plan':
                return real(session, state, batch, role, context)
            return {'summary': 'Verification unavailable', 'outcomes': [
                {'item': i['key'], 'status': 'no_change', 'evidence': 'Verification incomplete'}
                for i in context['feedback']], 'complete': False, 'checks': [], 'blocking': []}

        with patch('pr_review.owner_providers.invoke', side_effect=incomplete):
            for _ in range(2):
                result = dispatch(self.req)
                self.assertEqual('needs_attention', result['outcome'], result)
                self.assertEqual(2, len(self.owner.load()['batches']))
                self.assertFalse(execution_complete(self.owner.load()['batches'][-1]))
                self.assertTrue(self.t.f['pr']['draft'])
        self.assertEqual(calls, self.push.call_count)
        self.assertEqual(1, len(self.reviewed))


class BootstrapTest(IssueIntegrationTest):
    """Also exercise the -i path before any PR exists."""
    def setUp(self):
        super().setUp()
        self.f.update(pr=None, pulls=[], head=None)
        self.created = []

    def collect(self, target, repo):
        if self.f.get('pr'):
            return super().collect(target, repo)
        return copy.deepcopy(self.f)

    def page(self, repo, endpoint):
        if '/pulls?' in endpoint:
            return copy.deepcopy(self.created)
        return super().page(repo, endpoint)

    def api(self, endpoint, payload, method='POST'):
        if endpoint == 'repos/org/repo/pulls':
            self.assertTrue(payload['draft'])
            self.metadata['headRefName'] = payload['head']
            sha = self.git('rev-parse', payload['head'])
            self.git('update-ref', 'remote-feature', sha)
            self.f['pr'] = {'number': 7, 'state': 'open', 'assignees': [], 'draft': True,
                            'body': payload['body'], 'user': {'login': 'author'},
                            'head': {'ref': payload['head'], 'sha': sha, 'repo': {'full_name': 'org/repo'}},
                            'base': {'ref': payload['base']}}
            self.f['pulls'] = [self.f['pr']]
            self.created.append(copy.deepcopy(self.f['pr']))
            return self.f['pr']
        return super().api(endpoint, payload, method)

    def collect(self, target, repo):
        if self.f.get('pr'):
            sha = self.git('rev-parse', self.f['pr']['head']['ref'])
            self.metadata['headRefOid'] = sha
            self.git('update-ref', 'refs/pull/7/head', sha)
            self.f['head'] = sha
            self.f['pr']['head']['sha'] = sha
        return copy.deepcopy(self.f)

    def test_bootstrap_commit_is_empty_and_created_once(self):
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        state = json.loads((Path(result['session']) / 'session.json').read_text())
        sha = state['bootstrap_sha']
        self.assertEqual(self.base, self.git('show', '-s', '--format=%P', sha))
        self.assertEqual(self.git('rev-parse', self.base + '^{tree}'), self.git('rev-parse', sha + '^{tree}'))
        self.assertEqual(1, len(self.created))
        self.assertEqual('noop', dispatch({**self.req, 'yolo': True})['outcome'])
        self.assertEqual(1, len(self.created))

    def interrupted_bootstrap_after_base_change(self, *, wrong_message=False):
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.f['base'] = self.head
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        flow = Workflow(self.req, self.repo_object, self.f)
        self.assertEqual(self.head, flow.state['planning_base'])
        self.assertEqual(self.head, flow.state['planning_head'])
        # Stop after the real commit but before its SHA is recorded. No writes
        # may reach a remote or create a PR, including if recovery regresses.
        flow.writes = Mock()
        from pr_review.common import command as real_command

        def commit_then_interrupt(argv, **kwargs):
            self.assertEqual(['git', 'commit'], argv[:2])
            if wrong_message:
                kwargs['input_text'] = 'Unrelated commit'
            real_command(argv, **{**kwargs, 'timeout': 10})
            raise ApiError('interrupted', 'Stopped immediately after commit creation')

        with patch('pr_review.issues.command', side_effect=commit_then_interrupt):
            with self.assertRaises(ApiError) as caught:
                flow.bootstrap()
        self.assertEqual('interrupted', caught.exception.code)
        flow.writes.push.assert_not_called()
        flow.writes.create_pr.assert_not_called()
        saved = json.loads(flow.file.read_text())
        self.assertNotIn('bootstrap_sha', saved)
        self.assertEqual(self.head, saved['planning_head'])
        sha = self.git('rev-parse', 'HEAD', cwd=saved['planning_worktree'])
        self.assertNotEqual(self.head, sha)
        return flow, sha

    def test_interrupted_bootstrap_recovery_advances_planning_head(self):
        flow, sha = self.interrupted_bootstrap_after_base_change()
        for _ in range(2):
            # Reload durable state to exercise independent recovery/retry calls.
            flow = Workflow(self.req, self.repo_object, self.f)
            path = flow.workspace()
            saved = json.loads(flow.file.read_text())
            self.assertEqual(sha, saved['bootstrap_sha'])
            self.assertEqual(sha, saved['planning_head'])
            self.assertEqual([sha], saved['allowed_heads'])
            self.assertEqual(sha, self.git('rev-parse', 'HEAD', cwd=path))

    def test_interrupted_bootstrap_rejects_mismatched_message(self):
        flow, sha = self.interrupted_bootstrap_after_base_change(wrong_message=True)
        before = flow.file.read_text()
        flow = Workflow(self.req, self.repo_object, self.f)
        with self.assertRaises(ApiError) as caught:
            flow.workspace()
        self.assertEqual('workspace_changed', caught.exception.code)
        self.assertEqual(before, flow.file.read_text())
        self.assertEqual(sha, self.git('rev-parse', 'HEAD', cwd=flow.state['planning_worktree']))

    # Base tests that assume a PR exists are covered by IssueIntegrationTest.
    test_plan_cli_approval_real_owner_commit_push_and_noop = None
    test_comment_metadata_does_not_repeat_owner_implementation = None
    test_verified_noncommitting_review_does_not_repeat_on_active_retries = None
    test_post_commit_hook_edits_block_push_and_readiness_on_retries = None
    test_unverified_source_after_execution_blocks_push = None
    test_unverified_index_after_execution_blocks_push = None
    test_source_changes_during_review_block_publication_and_readiness = None
    test_cached_readiness_honors_retained_inspection_blocker = None
    test_rejected_push_retries_without_selected_feedback = None
    test_accepted_unconfirmed_push_recovers_without_selected_feedback = None
    test_remote_head_moves_during_review_blocks_readiness = None
    test_unconfirmed_remote_head_blocks_review = None
    test_unexpected_remote_commit_blocks_even_force_yolo = None
    test_approval_revoked_before_push_blocks = None
    test_managed_workspace_edits_require_adoption = None

    def test_bootstrap_hook_failure_never_creates_pr(self):
        hooks = self.repo / '.git' / 'hooks'
        hook = hooks / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(3, result['exit_code'], result)
        self.assertEqual([], self.created)

    def test_lost_pr_creation_response_recovers_without_second_pr(self):
        real = self.api
        def lose(endpoint, payload, method='POST'):
            result = real(endpoint, payload, method)
            if endpoint == 'repos/org/repo/pulls':
                raise ApiError('network', 'lost response')
            return result
        with patch('pr_review.issue_writes.Writes.api', side_effect=lose):
            result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(5, result['exit_code'], result)
        self.assertEqual(1, len(self.created))
        result = dispatch({**self.req, 'yolo': True})
        self.assertEqual(0, result['exit_code'], result)
        self.assertEqual(1, len(self.created))


class ReviewHandoffTest(unittest.TestCase):
    def make_flow(self):
        from types import SimpleNamespace
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        flow = object.__new__(Workflow)
        flow.path = Path(temp.name)
        flow.request = {}
        flow.repo = SimpleNamespace(cwd=flow.path, identity='github.com/org/repo')
        flow.state = {'configuration': {'reviewers': ['claude', 'codex'], 'processing_provider': 'codex'}, 'pr': 7,
                      'assessed_base': 'base-sha', 'remote_head': 'head-sha'}
        flow.save = lambda: None
        return flow

    def test_review_uses_shared_config_and_exact_draft_revision(self):
        from types import SimpleNamespace
        flow = self.make_flow()
        data = {'mutation_tracking': True, 'comments': [{'id': 'C1', 'status': 'unstaged', 'classification': 'NEW'}]}
        session = SimpleNamespace(root=flow.path, id='generation', manifest={'snapshot': {'base': flow.state['assessed_base'], 'head': flow.state['remote_head']}}, load=lambda: data, blockers=lambda *a, **k: [])
        with patch('pr_review.reviews.execute', return_value=session) as execute, patch('pr_review.owner_import.from_review', return_value={'items': ['local:C1']}) as imported:
            self.assertEqual(['local:C1'], flow.review())
        self.assertEqual(['claude', 'codex'], execute.call_args.args[0]['reviewers'])
        self.assertEqual('codex', execute.call_args.args[0]['processing_provider'])
        self.assertEqual(digest(data), imported.call_args.args[0]['revision'])

    def test_uncertain_review_cannot_be_silently_skipped(self):
        from types import SimpleNamespace
        flow = self.make_flow()
        data = {'mutation_tracking': True, 'comments': [{'id': 'C1', 'status': 'unstaged', 'classification': 'UNCERTAIN'}]}
        session = SimpleNamespace(root=flow.path, id='generation', manifest={'snapshot': {'base': flow.state['assessed_base'], 'head': flow.state['remote_head']}}, load=lambda: data, blockers=lambda *a, **k: ['Needs judgment'])
        with patch('pr_review.reviews.execute', return_value=session), patch('pr_review.owner_import.from_review') as imported:
            with self.assertRaises(ApiError) as error:
                flow.review()
        self.assertEqual('needs_attention', error.exception.code)
        imported.assert_not_called()


class CollectorTest(unittest.TestCase):
    git = test_owner.OwnerTest.git
    github = test_owner.OwnerTest.github
    model = test_owner.OwnerTest.model
    publish = test_owner.OwnerTest.publish
    request = test_owner.OwnerTest.request
    item = staticmethod(test_owner.OwnerTest.item)

    def setUp(self):
        test_owner.OwnerTest.setUp(self)
        self.repo_object = owner_state.OwnerSession({'session': self.path}).repo
        self.issue = facts()['issue']
        self.pull = {'number': 7, 'state': 'open', 'assignees': [], 'head': {'ref': 'feature', 'sha': self.head, 'repo': {'full_name': 'org/repo'}}, 'base': {'ref': 'main'}}
        self.page_data = [[]]
        def github(repo, endpoint, cwd, pages=False):
            if pages:
                return self.page_data
            if endpoint == 'user':
                return {'login': 'author'}
            if endpoint == 'repos/org/repo':
                return {'default_branch': 'main', 'permissions': {'push': True}}
            return copy.deepcopy(self.pull if '/pulls/' in endpoint else self.issue)
        p = patch('pr_review.issue_eligibility.github_json', side_effect=github)
        p.start()
        self.addCleanup(p.stop)
        p = patch('pr_review.issue_eligibility.connection', return_value=[{'number': 7, 'repository': {'nameWithOwner': 'org/repo'}}])
        p.start()
        self.addCleanup(p.stop)

    def collect(self):
        return collect_eligibility_facts({'issue': 12}, self.repo_object)

    def test_nonempty_branch_rejected(self):
        # main contains the current source commit; use its parent as the fetched base.
        self.git('branch', 'base', self.base)
        self.pull['base']['ref'] = 'base'
        f = self.collect()
        self.assertTrue(f['complete'], f)
        self.assertEqual('ineligible', evaluate_eligibility(f)['eligibility'])

    def test_empty_bootstrap_allowed_without_touching_checkout(self):
        empty = self.git('commit-tree', self.base + '^{tree}', '-p', self.base, '-m', 'empty')
        self.git('update-ref', 'refs/pull/7/head', empty)
        self.pull['head']['sha'] = empty
        (self.repo / 'source.txt').write_text('manual edit')
        f = self.collect()
        self.assertEqual('eligible', evaluate_eligibility(f)['eligibility'], f)
        self.assertEqual('manual edit', (self.repo / 'source.txt').read_text())
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))

    def test_code_then_revert_is_not_empty_history(self):
        self.git('branch', 'base', self.base)
        self.pull['base']['ref'] = 'base'
        revert = self.git('commit-tree', self.base + '^{tree}', '-p', self.head, '-m', 'revert')
        self.git('update-ref', 'refs/pull/7/head', revert)
        self.pull['head']['sha'] = revert
        f = self.collect()
        self.assertFalse(f['untouched'])

    def test_incomplete_pagination_is_unknown(self):
        self.page_data = {'not': 'paginated'}
        f = self.collect()
        self.assertFalse(f['complete'])
        self.assertEqual('indeterminate', evaluate_eligibility(f)['eligibility'])

    def test_head_changed_during_fetch_is_unknown(self):
        self.pull['head']['sha'] = self.base
        f = self.collect()
        self.assertFalse(f['complete'])

    def test_fork_pr_is_rejected(self):
        self.pull['head']['repo']['full_name'] = 'elsewhere/repo'
        f = self.collect()
        self.assertEqual('ineligible', evaluate_eligibility(f)['eligibility'])
