"""GitHub issue reads and compatibility normalization; no eligibility policy."""
import json
from .common import ApiError, command
from .pulls import github_json

def connection(repo, number, *, issue=True):
    """Read explicit closing relationships, including manual links, completely."""
    field = 'issue' if issue else 'pullRequest'
    relation = 'closedByPullRequestsReferences' if issue else 'closingIssuesReferences'
    extra = ', includeClosedPrs:true' if issue else ''
    cursor, nodes, seen = None, [], set()
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
            if not isinstance(result['nodes'], list):
                raise ValueError('Missing relationship nodes')
            nodes.extend(result['nodes'])
            page = result['pageInfo']
            if type(page['hasNextPage']) is not bool:
                raise ValueError('Missing pagination state')
            if not page['hasNextPage']:
                return nodes
            if not page['endCursor'] or page['endCursor'] in seen:
                raise ValueError('Pagination did not advance')
            cursor = page['endCursor']
            seen.add(cursor)
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('facts_incomplete', 'Cannot read complete issue/PR relationships.') from exc


def pages(repo, endpoint):
    value = github_json(repo.identity, endpoint, repo.cwd, pages=True)
    if not isinstance(value, list) or any(not isinstance(p, list) for p in value):
        raise ApiError('facts_incomplete', 'Incomplete GitHub pagination.')
    return [item for page in value for item in page]



class GitHubIssues:
    def account(self, repo):
        return repo.forge.account_from(repo.project, github_json(repo.identity, 'user', repo.cwd))

    def repository(self, repo):
        value = github_json(repo.identity, 'repos/' + repo.name, repo.cwd)
        return dict(default_branch=value['default_branch'], permission=value.get('permissions', {}).get('push'))

    def references(self, repo, number, *, issue=True):
        result = [dict(number=r['number'], project=repo.project.host + '/' + r['repository']['nameWithOwner'].lower(), raw=r)
                  for r in connection(repo, number, issue=issue)]
        identities = [(r['project'], r['number']) for r in result]
        if any(type(n) is not int or n <= 0 for _, n in identities) or len(set(identities)) != len(identities):
            raise ApiError('facts_incomplete', 'Invalid or duplicate relationship identities.')
        return result

    def issue(self, repo, number):
        value = github_json(repo.identity, f'repos/{repo.name}/issues/{number}', repo.cwd)
        if value.get('pull_request'):
            raise ApiError('invalid_request', f'#{number} in {repo.identity} is a pull request, not an issue. Use -p/--pr {number} to select that pull request, or pass its linked issue number with -i/--issue.')
        if type(value.get('number')) is not int or value.get('number') != number or (value.get('html_url') or '').lower() != f'https://{repo.identity}/issues/{number}':
            raise ApiError('facts_incomplete', 'Issue identity mismatch.')
        return value

    def change(self, repo, number):
        value = github_json(repo.identity, f'repos/{repo.name}/pulls/{number}', repo.cwd)
        if type(value.get('number')) is not int or value.get('number') != number:
            raise ApiError('facts_incomplete', 'PR identity mismatch.')
        return value

    def comments(self, repo, number):
        values = pages(repo, f'repos/{repo.name}/issues/{number}/comments')
        if any(not isinstance(c, dict) or type(c.get('id')) is not int
               or not isinstance(c.get('user'), dict) or not isinstance(c['user'].get('login'), str)
               or not isinstance(c.get('body'), str) for c in values):
            raise ApiError('facts_incomplete', 'Incomplete issue comments.')
        if len({c['id'] for c in values}) != len(values):
            raise ApiError('facts_incomplete', 'Duplicate issue comments.')
        return values

    def normalize(self, facts):
        if facts.get('complete') and not facts.get('link_error') and not isinstance(facts.get('pulls'), list):
            raise ApiError('facts_incomplete', 'Complete change relationships required.')
        def item(value, change=False):
            if value is None:
                return None
            if (not isinstance(value, dict) or type(value.get('number')) is not int
                    or value['number'] <= 0 or value.get('state') not in ('open', 'closed')):
                raise ApiError('facts_incomplete', 'Missing resource state.')
            if change and value['state'] == 'closed' and 'merged_at' not in value:
                raise ApiError('facts_incomplete', 'Missing merge status.')
            assigned = value.get('assignees')
            assignees = ([a['login'] for a in assigned] if isinstance(assigned, list)
                         and all(isinstance(a, dict) and isinstance(a.get('login'), str) and a['login'] for a in assigned) else None)
            result = dict(number=value['number'], state=value.get('state'), body=value.get('body'), assignees=assignees)
            if change:
                head, base = value.get('head') or {}, value.get('base') or {}
                if not isinstance(head, dict) or not isinstance(base, dict):
                    raise ApiError('facts_incomplete', 'Invalid branch facts.')
                project = (head.get('repo') or {}).get('full_name')
                result.update(merged=bool(value.get('merged_at')), source_project=facts['repository'].split('/')[0] + '/' + project.lower() if project else None,
                              source_branch=head.get('ref'), target_branch=base.get('ref'), head=head.get('sha'))
            return result
        result = dict(issue=item(facts.get('issue')), pr=item(facts.get('pr'), True),
                      pulls=[item(p, True) for p in facts.get('pulls', [])])
        if result['pr'] and not isinstance(result['pr']['head'], str):
            raise ApiError('facts_incomplete', 'Missing selected change head.')
        return result
