"""Read-only GitLab.com API transport and normalized review facts."""
import json
import re
from urllib.parse import quote
from .common import ApiError, command
from .forge_context import validate_change, validate_discussion, legacy_metadata
from .storage import write_json


class GitLab:
    capabilities = frozenset(('review',))
    executable = 'glab'

    def project(self, value):
        from .forge import Project
        if value.lower().startswith('gitlab.com/'):
            value = 'gitlab.com/' + value.split('/', 1)[1]
        if '://' in value or ('.' in value.split('/')[0] and not value.startswith('gitlab.com/')):
            raise ApiError('unsupported_forge', 'The GitLab pilot supports gitlab.com only.')
        if not value.startswith('gitlab.com/'):
            value = 'gitlab.com/' + value
        return Project('gitlab', 'gitlab.com', value.split('/', 1)[1].lower())

    def target(self, value):
        match = re.fullmatch(r'https://(gitlab\.com/.+)/-/merge_requests/([1-9][0-9]*)/?', str(value), re.IGNORECASE)
        if match:
            return self.project(match[1]), int(match[2])
        if not re.fullmatch(r'[1-9][0-9]*', str(value)):
            raise ApiError('invalid_request', 'Expected an MR number or GitLab.com merge request URL.')
        return None, int(value)

    def url(self, project, number, kind='change'):
        return f'https://{project.repository}/-/merge_requests/{number}'

    def head_ref(self, number):
        return f'refs/merge-requests/{number}/head'

    def discover(self, cwd):
        from .common import git
        from .pulls import repository_name
        values = {repository_name(url, 'gitlab') for url in git(['config', '--get-regexp', r'^remote\..*\.url$'], cwd=cwd).stdout.splitlines() for url in url.split()[1:2] if 'gitlab.com' in url}
        values.discard(None)
        if len(values) != 1:
            raise ApiError('invalid_request', 'Specify --repo gitlab.com/GROUP/PROJECT.')
        return self.project(values.pop())

    def cli_checks(self):
        return [(['glab', 'api', '--help'], ['--hostname', '--method'])]

    def auth_check(self, project):
        return ['glab', 'api', '--hostname', project.host, '--method', 'GET', 'user']

    def get(self, repo, endpoint):
        try:
            return json.loads(command(['glab', 'api', '--hostname', 'gitlab.com', '--method', 'GET', endpoint], cwd=repo.cwd).stdout)
        except ValueError as exc:
            raise ApiError('invalid_response', 'Invalid GitLab response.') from exc

    def pages(self, repo, endpoint):
        rows, seen = [], set()
        for page in range(1, 10001):
            value = self.get(repo, endpoint + ('&' if '?' in endpoint else '?') + f'per_page=100&page={page}')
            if not isinstance(value, list):
                raise ApiError('invalid_response', 'Incomplete GitLab collection.')
            for row in value:
                identity = row.get('id') if isinstance(row, dict) else None
                if type(identity) not in (str, int) or identity in seen:
                    raise ApiError('invalid_response', 'Invalid or repeated GitLab record.')
                seen.add(identity)
            rows.extend(value)
            if len(value) < 100:
                return rows
        raise ApiError('invalid_response', 'GitLab pagination limit exceeded.')

    def endpoint(self, repo):
        return f'projects/{quote(repo.project.path, safe="")}/merge_requests/{repo.pr}'

    def account(self, project, cwd):
        from types import SimpleNamespace
        value = self.get(SimpleNamespace(cwd=cwd), 'user')
        if not isinstance(value, dict) or not value.get('username') or not value.get('id'):
            raise ApiError('authentication_failed', 'Missing GitLab account identity.')
        return dict(forge='gitlab', host=project.host, username=value['username'], id=str(value['id']))

    def permission(self, repo):
        return None

    def change(self, repo):
        raw = self.get(repo, self.endpoint(repo))
        try:
            refs = raw['diff_refs']
            if raw['sha'] != refs['head_sha']:
                raise ValueError()
            if isinstance(raw.get('web_url'), str) and raw['web_url'].lower() != repo.url.lower():
                raise ApiError('invalid_request', 'MR project identity changed; use its canonical URL: ' + raw['web_url'])
            if raw['iid'] != repo.pr or any(not re.fullmatch('[0-9a-f]{40,64}', refs[k]) for k in ('base_sha', 'start_sha', 'head_sha')):
                raise ValueError()
            source_project, warnings = None, []
            if raw['source_project_id'] is not None:
                try:
                    source = self.get(repo, f"projects/{raw['source_project_id']}")
                    source_project = self.project(source['path_with_namespace']).saved()
                except ApiError as exc:
                    if exc.code != 'command_failed' or not re.search(r'\b(?:403|404)\b', str(exc)):
                        raise
                    warnings.append('Source project is inaccessible; reviewing the retained target MR ref.')
            else:
                warnings.append('Source project was deleted; reviewing the retained target MR ref.')
            value = dict(id=raw['iid'], url=raw['web_url'], title=raw['title'], body=raw['description'] or '', author=raw['author']['username'],
                         state={'opened': 'open', 'locked': 'open', 'closed': 'closed', 'merged': 'merged'}[raw['state']], draft=raw['draft'],
                         source=dict(project=source_project, branch=raw['source_branch'], head=refs['head_sha']),
                         target=dict(project=repo.project.saved(), branch=raw['target_branch'], head=refs['start_sha']),
                         cross_project=raw['source_project_id'] != raw['target_project_id'], diff_refs=refs, warnings=warnings)
            return validate_change(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('invalid_response', 'Incomplete GitLab MR metadata or diff references.') from exc

    def metadata(self, repo, destination=None):
        value = legacy_metadata(self.change(repo))
        if destination:
            write_json(destination, value)
        return value

    def initial_discussion(self, repo):
        return [], []

    def discussion(self, repo):
        raw = self.pages(repo, self.endpoint(repo) + '/discussions')
        items = []
        head = self.change(repo)['source']['head']
        try:
            for thread in raw:
                if type(thread.get('individual_note')) is not bool or not isinstance(thread.get('id'), str):
                    raise ValueError()
                notes = thread['notes']
                if not isinstance(notes, list):
                    raise ValueError()
                if any(not isinstance(n, dict) or type(n.get('system')) is not bool or type(n.get('resolvable')) is not bool or (n['resolvable'] and type(n.get('resolved')) is not bool) for n in notes):
                    raise ValueError()
                if thread['individual_note'] and len(notes) != 1:
                    raise ValueError()
                if not notes:
                    raise ValueError()
                comments = [dict(id=n['id'], author=n['author']['username'], body=n['body'], url=repo.url + '#note_' + str(n['id']), created_at=n['created_at'], updated_at=n['updated_at'], system=n['system']) for n in notes]
                root = notes[0]
                if thread['individual_note']:
                    items.append(dict(key='comment:' + str(root['id']), kind='comment', comments=comments))
                else:
                    pos = root.get('position') or {}
                    if not isinstance(pos, dict):
                        raise ValueError()
                    if pos and any(not isinstance(pos.get(k), str) or not re.fullmatch('[0-9a-f]{40,64}', pos[k]) for k in ('base_sha', 'start_sha', 'head_sha')):
                        raise ValueError()
                    items.append(dict(key='thread:' + thread['id'], kind='thread', root_id=root['id'], url=comments[0]['url'], comments=comments,
                                      path=(pos.get('new_path') if pos.get('new_line') is not None else pos.get('old_path')) or pos.get('new_path'),
                                      line=pos.get('new_line') if pos.get('new_line') is not None else pos.get('old_line'),
                                      side=('new' if pos.get('new_line') is not None else 'old' if pos.get('old_line') is not None else None),
                                      is_resolved=all(n.get('resolved', False) for n in notes if n.get('resolvable')) if root.get('resolvable') else False,
                                      is_outdated=bool(pos and pos.get('head_sha') != head), can_reply=False, can_resolve=False, can_unresolve=False))
            return validate_discussion(dict(format_version=1, items=sorted(items, key=lambda x: x['key']))), {'discussions': raw}
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('incomplete_discussion', 'Incomplete GitLab discussion.') from exc

    def review_status(self, repo):
        return {'available': False}

    def review_context(self, repo, change, issues, reviews):
        warnings = list(change.get('warnings', []))
        discussion_available = True
        try:
            discussion, diagnostics = self.discussion(repo)
        except ApiError as exc:
            if exc.code == 'cancelled':
                raise
            discussion, diagnostics = {'items': []}, {}
            discussion_available = False
            warnings.append('GitLab discussion context unavailable; complete discussion matching will be retried before completion.')
        ci = dict(available=False, complete=False, coverage='unavailable', checks=[])
        try:
            pipelines = self.pages(repo, self.endpoint(repo) + '/pipelines')
            diagnostics['pipelines'] = pipelines
            checks, omitted = [], []
            for p in pipelines:
                if p['sha'] != change['source']['head']:
                    continue
                state = {'success': 'passed', 'failed': 'failed', 'canceled': 'failed', 'running': 'pending', 'pending': 'pending', 'created': 'pending', 'manual': 'pending', 'scheduled': 'pending', 'waiting_for_resource': 'pending', 'preparing': 'pending'}.get(p['status'])
                if state is None:
                    omitted.append(str(p['id']) + ': ' + str(p['status']))
                    continue
                checks.append(dict(name='Pipeline ' + str(p['id']), state=state, url=p['web_url']))
            ci = dict(available=bool(checks), complete=bool(checks) and not omitted, coverage=('MR pipelines for reviewed head' if checks else 'No mapped pipeline available for reviewed head') + ('; unmapped: ' + ', '.join(omitted) if omitted else ''), checks=checks)
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ApiError) and exc.code == 'cancelled':
                raise
            warnings.append('GitLab CI context unavailable or incomplete.')
        return dict(change=change, timeline=[dict(c, **({'anchor': {'path': i['path'], 'line': i['line'], 'side': i.get('side')}} if i.get('path') else {})) for i in discussion['items'] for c in i['comments']], reviews=[], inline=[], inline_available=discussion_available, ci=ci, warnings=warnings), diagnostics
