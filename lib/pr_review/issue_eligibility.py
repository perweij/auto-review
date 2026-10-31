"""Reusable, non-model eligibility collection and pure policy evaluation."""
from dataclasses import dataclass
import json

from .common import ApiError, command, digest
from .repository import Repository
from .pulls import github_json


@dataclass(frozen=True)
class EligibilityPolicy:
    force: bool = False


def connection(repo, number, *, issue=True):
    """Read explicit closing relationships, including manual links, completely."""
    field = 'issue' if issue else 'pullRequest'
    relation = 'closedByPullRequestsReferences' if issue else 'closingIssuesReferences'
    extra = ', includeClosedPrs:true' if issue else ''
    cursor, nodes = None, []
    while True:
        query = ('query($owner:String!,$name:String!,$n:Int!,$after:String){repository(owner:$owner,name:$name){'
                 + field + '(number:$n){' + relation + '(first:100,after:$after' + extra + ')'
                 '{nodes{number repository{nameWithOwner}} pageInfo{hasNextPage endCursor}}}}}')
        owner, name = repo.name.split('/')
        payload = {'query': query, 'variables': {'owner': owner, 'name': name, 'n': number, 'after': cursor}}
        value = json.loads(command(['gh', 'api', '--hostname', repo.host, 'graphql', '--input', '-'],
                                   cwd=repo.cwd, input_text=json.dumps(payload)).stdout)
        try:
            if value.get('errors'):
                raise ValueError(str(value['errors']))
            result = value['data']['repository'][field][relation]
            nodes.extend(result['nodes'])
            page = result['pageInfo']
            if not page['hasNextPage']:
                return nodes
            if not page['endCursor'] or page['endCursor'] == cursor:
                raise ValueError('Pagination did not advance')
            cursor = page['endCursor']
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('facts_incomplete', 'Cannot read complete issue/PR relationships.') from exc


def pages(repo, endpoint):
    value = github_json(repo.identity, endpoint, repo.cwd, pages=True)
    if not isinstance(value, list) or any(not isinstance(p, list) for p in value):
        raise ApiError('facts_incomplete', 'Incomplete GitHub pagination.')
    return [item for page in value for item in page]


def collect_eligibility_facts(target, repository_context, prefetched=None):
    """Fetch only dedicated snapshot refs; never change working files or publish.

    Prefetched complete facts are accepted verbatim for scanner/batch callers.
    """
    if prefetched is not None:
        return prefetched
    repo = repository_context
    try:
        get = lambda path: github_json(repo.identity, path, repo.cwd)
        viewer = get('user')['login']
        info = get('repos/' + repo.name)
        issue_number = target.get('issue')
        if not issue_number:
            refs = connection(repo, int(target['pr']), issue=False)
            if len(refs) != 1 or refs[0]['repository']['nameWithOwner'].lower() != repo.name:
                return {'complete': True, 'repository': repo.identity, 'viewer': viewer,
                        'link_error': 'issue_link_missing' if not refs else 'issue_link_ambiguous'}
            issue_number = refs[0]['number']
        issue = get(f'repos/{repo.name}/issues/{issue_number}')
        if issue.get('number') != issue_number or (issue.get('html_url') or '').lower() != f'https://{repo.identity}/issues/{issue_number}':
            raise ApiError('facts_incomplete', 'Issue identity mismatch.')
        if issue.get('pull_request'):
            raise ApiError('invalid_request', 'The issue selector names a pull request.')
        linked = connection(repo, issue_number)
        if any(r['repository']['nameWithOwner'].lower() != repo.name for r in linked):
            return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'issue': issue,
                    'pull_references': linked, 'link_error': 'foreign_pr_unsupported'}
        pulls = [get(f'repos/{repo.name}/pulls/{r["number"]}') for r in linked]
        if target.get('pr') and int(target['pr']) not in [p['number'] for p in pulls]:
            return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'issue': issue,
                    'pull_references': linked, 'link_error': 'issue_link_missing'}
        active = [p for p in pulls if p['state'] == 'open']
        pr = next((p for p in pulls if p['number'] == target.get('pr')), None) if target.get('pr') else (active[0] if len(active) == 1 else None)
        base_branch = pr['base']['ref'] if pr else info['default_branch']
        namespace = 'refs/fix-issue/' + digest([repo.identity, issue_number])[:20]
        repo.git('fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', repo.remote,
                 f'+refs/heads/{base_branch}:{namespace}/base')
        base = repo.git('rev-parse', namespace + '/base').stdout.strip()
        head, untouched = None, True
        if pr:
            repo.git('fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', repo.remote,
                     f'+refs/pull/{pr["number"]}/head:{namespace}/head')
            head = repo.git('rev-parse', namespace + '/head').stdout.strip()
            if head != pr['head']['sha']:
                raise ApiError('facts_incomplete', 'PR changed during fetch.')
            commits = repo.git('rev-list', head, '--not', base).stdout.splitlines()
            for sha in commits:
                parents = repo.git('show', '-s', '--format=%P', sha).stdout.split()
                if len(parents) != 1 or repo.git('rev-parse', sha + '^{tree}').stdout != repo.git('rev-parse', parents[0] + '^{tree}').stdout:
                    untouched = False
                    break
        return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'issue': issue, 'pr': pr,
                'pulls': pulls, 'pull_references': linked,
                'base_branch': base_branch, 'base': base, 'head': head, 'untouched': untouched,
                'permission': info.get('permissions', {}).get('push'),
                'comments': pages(repo, f'repos/{repo.name}/issues/{issue_number}/comments')}
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        return {'complete': False, 'repository': repo.identity, 'error': str(exc), 'error_code': getattr(exc, 'code', 'facts_incomplete')}


def evaluate_eligibility(facts, policy=None):
    """Pure structural eligibility. Unknown facts never count as passing."""
    policy = policy or EligibilityPolicy()
    checks = []
    def check(code, passed, message, overridable=False):
        checks.append({'code': code, 'outcome': 'unknown' if passed is None else 'pass' if passed else 'fail',
                       'message': message, 'overridable': overridable,
                       'overridden': bool(not passed and overridable and policy.force)})
    check('facts_incomplete', True if facts.get('complete') else None, facts.get('error', 'Complete facts required.'))
    issue, pr, workflow = facts.get('issue'), facts.get('pr'), facts.get('workflow')
    if facts.get('link_error'):
        message = ('Associated PRs must belong to the issue repository.'
                   if facts['link_error'] == 'foreign_pr_unsupported' else 'Select one explicitly linked issue and PR.')
        check(facts['link_error'], False, message)
    elif facts.get('complete'):
        check('issue_closed', bool(issue and issue.get('state') == 'open'), 'Issue must be open.')
        check('description_empty', bool(issue and (issue.get('body') or '').strip()), 'Issue description is required.')
        check('permission_denied', facts.get('permission'), 'Repository push permission is required; server rules may still reject writes.')
        viewer = facts.get('viewer', '').lower()
        for kind, item in (('issue', issue), ('pr', pr)):
            if item:
                assignees = item.get('assignees')
                check(kind + '_assigned_elsewhere', None if not isinstance(assignees, list) else
                      all(a.get('login', '').lower() == viewer for a in assignees),
                      kind.upper() + ' must be unassigned or assigned only to self.', True)
        pulls = facts.get('pulls', [])
        check('multiple_open_prs', sum(p.get('state') == 'open' for p in pulls) <= 1, 'Only one open associated PR is supported.')
        check('prior_merged_pr', not any(p.get('merged_at') for p in pulls if not workflow or p['number'] != workflow.get('pr')),
              'An earlier associated PR was merged; inspect manually.')
        if pr:
            check('pr_closed', pr.get('state') == 'open', 'PR must be open.')
            check('fork_unsupported', (pr.get('head', {}).get('repo') or {}).get('full_name', '').lower() == facts['repository'].split('/', 1)[1], 'Fork PRs are unsupported.')
            if workflow and workflow.get('managed'):
                expected = {workflow.get('remote_head')}
                expected.update(a.get('sha') for a in workflow.get('actions', {}).values() if a.get('status') == 'submitting' and a.get('sha'))
                check('unexpected_head', facts['head'] in expected, 'PR head must match the last confirmed or pending workflow push.')
                check('target_changed', pr['head']['ref'] == workflow.get('remote_branch') and pr['base']['ref'] == workflow.get('base_branch'), 'PR branches must retain their recorded identities.')
            else:
                check('branch_contains_changes', facts.get('untouched'), 'First adoption permits only empty, single-parent commits.')
    failed = any(c['outcome'] == 'fail' and not c['overridden'] for c in checks)
    unknown = any(c['outcome'] == 'unknown' for c in checks)
    phase = (workflow or {}).get('phase')
    return {'eligibility': 'ineligible' if failed else 'indeterminate' if unknown else 'eligible',
            'disposition': 'complete' if phase == 'complete' else 'waiting_for_input' if phase == 'waiting' else 'resumable' if workflow else 'new',
            'next_action': (workflow or {}).get('next_action', 'assess'), 'checks': checks,
            'identity': {'repository': facts.get('repository'), 'issue': (issue or {}).get('number'), 'pr': (pr or {}).get('number')},
            'snapshot': {'base': facts.get('base'), 'head': facts.get('head'), 'issue': digest(issue),
                         'discussion': digest(facts.get('comments', [])), 'workflow_revision': (workflow or {}).get('sequence')}}
