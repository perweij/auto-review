"""Normalized eligibility keeps unknown facts distinct from negative facts."""
import copy
import unittest
from unittest.mock import patch
import test_issues
from pr_review.forge_github_issues import GitHubIssues
from pr_review.issue_eligibility import collect_eligibility_facts, evaluate_eligibility, EligibilityPolicy


class FactsTest(unittest.TestCase):
    def test_normalized_and_legacy_results_are_identical(self):
        facts = test_issues.facts()
        self.assertEqual(evaluate_eligibility(facts), evaluate_eligibility({**facts, 'normalized': GitHubIssues().normalize(facts)}))

    def test_unknown_assignments_cannot_be_forced(self):
        for value in (None, {}, [None], [{}], [{'login': ''}]):
            facts = test_issues.facts()
            facts['issue']['assignees'] = value
            self.assertEqual(evaluate_eligibility(facts, EligibilityPolicy(True))['eligibility'], 'indeterminate')

    def test_prefetched_has_no_io_and_is_not_modified(self):
        facts = test_issues.facts()
        before = copy.deepcopy(facts)
        with patch('pr_review.forge_github_issues.github_json', side_effect=AssertionError('Network')), patch('pr_review.forge_github_issues.command', side_effect=AssertionError('Command')):
            self.assertEqual(collect_eligibility_facts({}, None, facts), before)
            evaluate_eligibility(facts)
        self.assertEqual(facts, before)

    def test_conflicting_normalized_facts_are_unknown(self):
        facts = test_issues.facts()
        facts['normalized'] = GitHubIssues().normalize(facts)
        facts['issue']['state'] = 'closed'
        self.assertEqual(evaluate_eligibility(facts)['eligibility'], 'indeterminate')

    def test_incomplete_relationships_are_unknown(self):
        facts = test_issues.facts()
        facts.pop('pulls')
        self.assertEqual(evaluate_eligibility(facts)['eligibility'], 'indeterminate')

    def test_null_branch_objects_do_not_crash(self):
        facts = test_issues.facts()
        facts['pr'] = dict(number=7, state='open', head=None, base=None, assignees=[])
        facts['pulls'] = [facts['pr']]
        self.assertNotEqual(evaluate_eligibility(facts)['eligibility'], 'eligible')

    def test_missing_merge_status_of_closed_change_is_unknown(self):
        facts = test_issues.facts()
        facts['pulls'] = [dict(number=7, state='closed', assignees=[])]
        self.assertEqual(evaluate_eligibility(facts)['eligibility'], 'indeterminate')

    def test_string_pr_selector_preserves_selected_change_checks(self):
        fixture = test_issues.EligibilityRelationshipTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.linked = [fixture.ref(7)]
        fixture.pulls[7]['head']['repo']['full_name'] = 'someone/fork'
        facts = collect_eligibility_facts({'pr': '7'}, fixture.repo)
        self.assertEqual(facts['pr']['number'], 7)
        self.assertEqual(evaluate_eligibility(facts)['eligibility'], 'ineligible')

    def test_adapter_relationship_pagination_rejects_cycles(self):
        import json
        from types import SimpleNamespace
        from pr_review.forge_github_issues import connection
        from pr_review.common import ApiError
        repo = SimpleNamespace(name='org/repo', host='github.com', cwd='.')
        def response(cursor):
            return SimpleNamespace(stdout=json.dumps({'data': {'repository': {'issue': {'closedByPullRequestsReferences':
                {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': cursor}}}}}}))
        with patch('pr_review.forge_github_issues.command', side_effect=[response('a'), response('b'), response('a')]):
            with self.assertRaises(ApiError):
                connection(repo, 12)

    def test_shared_collector_accepts_adapter_owned_non_github_facts(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from pr_review.forge import Project
        project = Project('github', 'github.com', 'org/repo')
        issue = dict(number=12, title='Fix', body='Required behavior', state='open', opaque_assignment='self')
        normalized = dict(issue=dict(number=12, state='open', body='Required behavior', assignees=['author']), pr=None, pulls=[])
        reader = Mock()
        reader.account.return_value = dict(username='author')
        reader.repository.return_value = dict(default_branch='main', permission=True)
        reader.references.return_value = []
        reader.issue.return_value = issue
        reader.comments.return_value = []
        reader.normalize.return_value = normalized
        forge = SimpleNamespace(issues=reader, project=lambda value: project)
        repo = SimpleNamespace(forge=forge, project=project, identity=project.repository, remote='origin',
                               git=Mock(return_value=SimpleNamespace(stdout='base')))
        facts = collect_eligibility_facts({'issue': 12}, repo)
        self.assertTrue(facts['complete'], facts)
        with patch('pr_review.forge.adapter', return_value=forge):
            self.assertEqual(evaluate_eligibility(facts)['eligibility'], 'eligible')
        self.assertEqual(facts['issue'], issue)
