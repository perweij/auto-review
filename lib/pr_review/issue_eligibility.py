"""Reusable, non-model eligibility collection and pure policy evaluation."""
from dataclasses import dataclass

from .common import ApiError, digest


@dataclass(frozen=True)
class EligibilityPolicy:
    force: bool = False


def collect_eligibility_facts(target, repository_context, prefetched=None):
    """Fetch only dedicated snapshot refs; never change working files or publish.

    Prefetched complete facts are accepted verbatim for scanner/batch callers.
    """
    if prefetched is not None:
        return prefetched
    repo = repository_context
    try:
        if not hasattr(repo.forge, 'issues'):
            raise ApiError('unsupported_operation', 'Issue facts are unsupported by this forge.')
        reader = repo.forge.issues
        account = reader.account(repo)
        viewer = account['username']
        info = reader.repository(repo)
        issue_number = target.get('issue')
        if not issue_number:
            refs = reader.references(repo, int(target['pr']), issue=False)
            if len(refs) != 1 or refs[0]['project'] != repo.identity:
                return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'forge_account': account,
                        'link_error': 'issue_link_missing' if not refs else 'issue_link_ambiguous'}
            issue_number = refs[0]['number']
        issue = reader.issue(repo, issue_number)
        linked = reader.references(repo, issue_number)
        if any(r['project'] != repo.identity for r in linked):
            return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'forge_account': account, 'issue': issue,
                    'pull_references': [r['raw'] for r in linked], 'link_error': 'foreign_pr_unsupported'}
        pulls = [reader.change(repo, r["number"]) for r in linked]
        if target.get('pr') and int(target['pr']) not in [p['number'] for p in pulls]:
            return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'forge_account': account, 'issue': issue,
                    'pull_references': [r['raw'] for r in linked], 'link_error': 'issue_link_missing'}
        active = [p for p in pulls if p['state'] == 'open']
        pr = next((p for p in pulls if p['number'] == int(target['pr'])), None) if target.get('pr') else (active[0] if len(active) == 1 else None)
        normalized = reader.normalize(dict(repository=repo.identity, issue=issue, pr=pr, pulls=pulls))
        selected = normalized['pr']
        base_branch = selected['target_branch'] if selected else info['default_branch']
        repo.git('check-ref-format', 'refs/heads/' + base_branch)
        namespace = 'refs/fix-issue/' + digest([repo.identity, issue_number])[:20]
        repo.git('fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', repo.remote,
                 f'+refs/heads/{base_branch}:{namespace}/base')
        base = repo.git('rev-parse', namespace + '/base').stdout.strip()
        head, untouched = None, True
        if pr:
            repo.git('fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', repo.remote,
                     f'+{repo.forge.head_ref(pr["number"])}:{namespace}/head')
            head = repo.git('rev-parse', namespace + '/head').stdout.strip()
            if head != selected['head']:
                raise ApiError('facts_incomplete', 'PR changed during fetch.')
            commits = repo.git('rev-list', head, '--not', base).stdout.splitlines()
            for sha in commits:
                parents = repo.git('show', '-s', '--format=%P', sha).stdout.split()
                if len(parents) != 1 or repo.git('rev-parse', sha + '^{tree}').stdout != repo.git('rev-parse', parents[0] + '^{tree}').stdout:
                    untouched = False
                    break
        return {'complete': True, 'repository': repo.identity, 'viewer': viewer, 'forge_account': account, 'issue': issue, 'pr': pr,
                'forge_project': repo.project.saved(), 'normalized': normalized, 'pulls': pulls, 'pull_references': [r['raw'] for r in linked],
                'base_branch': base_branch, 'base': base, 'head': head, 'untouched': untouched,
                'permission': info.get('permission'),
                'comments': reader.comments(repo, issue_number)}
    except (ApiError, KeyError, TypeError, ValueError, AttributeError) as exc:
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
    from .forge import adapter, restore
    try:
        forge = adapter(restore(facts).forge) if facts.get('repository') else None
        if forge is not None and not hasattr(forge, 'issues'):
            raise ApiError('unsupported_operation', 'Issue facts are unsupported by this forge.')
        normalized = forge.issues.normalize(facts) if forge else {}
        if 'normalized' in facts and facts['normalized'] != normalized:
            raise ApiError('facts_incomplete', 'Normalized facts conflict with their compatibility snapshot.')
    except (ApiError, KeyError, TypeError, ValueError, AttributeError) as exc:
        phase = (facts.get('workflow') or {}).get('phase')
        return {'eligibility': 'indeterminate', 'disposition': 'complete' if phase == 'complete' else 'waiting_for_input' if phase == 'waiting' else 'resumable' if facts.get('workflow') else 'new',
                'next_action': (facts.get('workflow') or {}).get('next_action', 'assess'),
                'checks': [{'code': 'facts_incomplete', 'outcome': 'unknown', 'message': facts.get('error', str(exc)), 'overridable': False, 'overridden': False}],
                'identity': {'repository': facts.get('repository'), 'issue': None, 'pr': None},
                'snapshot': {'base': facts.get('base'), 'head': facts.get('head'), 'issue': digest(facts.get('issue')),
                             'discussion': digest(facts.get('comments', [])), 'workflow_revision': (facts.get('workflow') or {}).get('sequence')}}
    issue, pr, workflow = normalized.get('issue'), normalized.get('pr'), facts.get('workflow')
    if facts.get('link_error'):
        message = ('Associated PRs must belong to the issue repository.'
                   if facts['link_error'] == 'foreign_pr_unsupported' else 'Select one explicitly linked issue and PR.')
        check(facts['link_error'], False, message)
    elif facts.get('complete'):
        check('issue_closed', bool(issue and issue.get('state') == 'open'), 'Issue must be open.')
        check('description_empty', bool(issue and (issue.get('body') or '').strip()), 'Issue description is required.')
        check('permission_denied', facts.get('permission') if type(facts.get('permission')) is bool else None, 'Repository push permission is required; server rules may still reject writes.')
        viewer = facts.get('viewer', '').lower()
        for kind, item in (('issue', issue), ('pr', pr)):
            if item:
                assignees = item.get('assignees')
                check(kind + '_assigned_elsewhere', None if not isinstance(assignees, list) else
                      all(a.lower() == viewer for a in assignees),
                      kind.upper() + ' must be unassigned or assigned only to self.', True)
        pulls = normalized.get('pulls', [])
        check('multiple_open_prs', sum(p.get('state') == 'open' for p in pulls) <= 1, 'Only one open associated PR is supported.')
        check('prior_merged_pr', not any(p.get('merged') for p in pulls if not workflow or p['number'] != workflow.get('pr')),
              'An earlier associated PR was merged; inspect manually.')
        if pr:
            check('pr_closed', pr.get('state') == 'open', 'PR must be open.')
            check('fork_unsupported', pr.get('source_project') == facts['repository'], 'Fork PRs are unsupported.')
            if workflow and workflow.get('managed'):
                expected = {workflow.get('remote_head')}
                expected.update(a.get('sha') for a in workflow.get('actions', {}).values() if a.get('status') == 'submitting' and a.get('sha'))
                check('unexpected_head', facts['head'] in expected, 'PR head must match the last confirmed or pending workflow push.')
                check('target_changed', pr['source_branch'] == workflow.get('remote_branch') and pr['target_branch'] == workflow.get('base_branch'), 'PR branches must retain their recorded identities.')
            else:
                check('branch_contains_changes', facts.get('untouched'), 'First adoption permits only empty, single-parent commits.')
    failed = any(c['outcome'] == 'fail' and not c['overridden'] for c in checks)
    unknown = any(c['outcome'] == 'unknown' for c in checks)
    phase = (workflow or {}).get('phase')
    return {'eligibility': 'ineligible' if failed else 'indeterminate' if unknown else 'eligible',
            'disposition': 'complete' if phase == 'complete' else 'waiting_for_input' if phase == 'waiting' else 'resumable' if workflow else 'new',
            'next_action': (workflow or {}).get('next_action', 'assess'), 'checks': checks,
            'identity': {'repository': facts.get('repository'), 'issue': (issue or {}).get('number'), 'pr': (pr or {}).get('number')},
            'snapshot': {'base': facts.get('base'), 'head': facts.get('head'), 'issue': digest(facts.get('issue')),
                         'discussion': digest(facts.get('comments', [])), 'workflow_revision': (workflow or {}).get('sequence')}}
