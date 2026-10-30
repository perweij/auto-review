"""Issue eligibility, publication recovery, and real Owner integration."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review.common import ApiError, digest
from pr_review.issue_eligibility import evaluate_eligibility, EligibilityPolicy, collect_eligibility_facts
from pr_review.issues import Workflow, approval_comment, dispatch
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

    # Base tests that assume a PR exists are covered by IssueIntegrationTest.
    test_plan_cli_approval_real_owner_commit_push_and_noop = None
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
        flow.state = {'configuration': {'reviewers': ['claude', 'codex'], 'processing_provider': 'codex'}, 'pr': 7}
        flow.save = lambda: None
        return flow

    def test_review_uses_shared_config_and_exact_draft_revision(self):
        from types import SimpleNamespace
        flow = self.make_flow()
        data = {'mutation_tracking': True, 'comments': [{'id': 'C1', 'status': 'unstaged', 'classification': 'NEW'}]}
        session = SimpleNamespace(root=flow.path, id='generation', load=lambda: data, blockers=lambda *a, **k: [])
        with patch('pr_review.reviews.execute', return_value=session) as execute, patch('pr_review.owner_import.from_review', return_value={'items': ['local:C1']}) as imported:
            self.assertEqual(['local:C1'], flow.review())
        self.assertEqual(['claude', 'codex'], execute.call_args.args[0]['reviewers'])
        self.assertEqual('codex', execute.call_args.args[0]['processing_provider'])
        self.assertEqual(digest(data), imported.call_args.args[0]['revision'])

    def test_uncertain_review_cannot_be_silently_skipped(self):
        from types import SimpleNamespace
        flow = self.make_flow()
        data = {'mutation_tracking': True, 'comments': [{'id': 'C1', 'status': 'unstaged', 'classification': 'UNCERTAIN'}]}
        session = SimpleNamespace(root=flow.path, id='generation', load=lambda: data, blockers=lambda *a, **k: ['Needs judgment'])
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
