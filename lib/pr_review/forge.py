"""Forge identity and capabilities for GitHub and the read-only GitLab pilot."""
from dataclasses import asdict, dataclass
import json
import re

from .common import ApiError, command, digest


@dataclass(frozen=True)
class Project:
    forge: str
    host: str
    path: str

    def __post_init__(self):
        object.__setattr__(self, 'host', self.host.lower())
        if (not re.fullmatch(r'[a-z][a-z0-9_-]*', self.forge) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', self.host)
                or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part)
                       or part in ('.', '..') or part.startswith('-') for part in self.path.split('/'))
                or len(self.path.split('/')) < 2):
            raise ApiError('invalid_request', 'Invalid forge project identity.')

    def saved(self):
        return asdict(self)

    @property
    def repository(self):
        return self.host + '/' + self.path

    def resource_key(self, kind, number):
        if self.forge == 'github' and kind == 'change':
            return str(number)
        return digest([self.saved(), kind, number])

    @property
    def storage_key(self):
        return self.repository if self.forge == 'github' else self.forge + '/' + digest(self.saved())


class GitHub:
    @property
    def conversations(self):
        from .forge_github_conversations import GitHubConversations
        return GitHubConversations()

    def commit_url(self, repository, commit):
        return f'https://{repository}/commit/{commit}'

    @property
    def publication(self):
        from .forge_github_publication import GitHubPublication
        return GitHubPublication()

    capabilities = frozenset(('review', 'publication', 'owner', 'issues'))
    executable = 'gh'

    def change_from_legacy(self, value, project):
        from .forge_github import change_from
        return change_from(value, project)

    def change(self, repository):
        return self.change_from_legacy(repository.metadata(), repository.project)

    def permission(self, repository):
        result = command(['gh', 'repo', 'view', repository.identity, '--json', 'viewerPermission',
                          '--jq', '.viewerPermission'], cwd=repository.cwd, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def review_context(self, repository, change, issues, reviews):
        from .forge_github import review_context
        return review_context(repository, change, issues, reviews)

    def initial_discussion(self, repository):
        return (repository.pages(f'repos/{repository.name}/issues/{repository.pr}/comments'),
                repository.pages(f'repos/{repository.name}/pulls/{repository.pr}/reviews'))

    def owner_context(self, repository):
        from .forge_github import owner_context
        return owner_context(repository)

    def list_changes(self, repository, cwd):
        from .forge_github import list_changes
        return list_changes(repository, cwd)

    def listed_change(self, repository, number, cwd):
        from .forge_github import listed_change
        return listed_change(repository, number, cwd)

    def anchors(self, diagnostics):
        return {c['id']: {'diff_hunk': c.get('diff_hunk', ''),
                         'position': {key: c.get(key) for key in ('commit_id', 'original_commit_id', 'path', 'line', 'original_line', 'side', 'start_line', 'start_side')}}
                for c in diagnostics.get('inline', [])}

    def metadata(self, repository, destination=None):
        from .forge_github import metadata
        return metadata(repository, destination)

    def pages(self, repository, endpoint, kind="array"):
        from .forge_github import pages
        return pages(repository, endpoint, kind)

    def graphql(self, repository, query, **variables):
        from .forge_github import graphql
        return graphql(repository, query, **variables)

    def discussion(self, repository):
        from .forge_github import fetch
        return fetch(repository)

    def review_status(self, repository):
        from .forge_github import review_status
        return review_status(repository)

    def project(self, repository):
        parts = repository.split('/')
        if len(parts) == 2:
            parts.insert(0, 'github.com')
        if len(parts) != 3:
            raise ApiError('invalid_request', 'Repository must be [HOST/]OWNER/REPO')
        return Project('github', parts[0].lower(), '/'.join(parts[1:]).lower())

    def target(self, target):
        target = str(target)
        match = re.fullmatch(r'https://([A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?', target)
        number = match[2] if match else target
        if not re.fullmatch(r'[1-9][0-9]*', number):
            raise ApiError('invalid_request', 'Expected a positive PR number or GitHub pull request URL')
        return (self.project(match[1]) if match else None), int(number)

    def url(self, project, number, kind='change'):
        return f'https://{project.repository}/{"issues" if kind == "issue" else "pull"}/{number}'

    def head_ref(self, number):
        return f'refs/pull/{number}/head'

    def discover(self, cwd):
        from .repository import normalize_remote
        value = json.loads(command(['gh', 'repo', 'view', '--json', 'nameWithOwner,url'], cwd=cwd).stdout)
        return self.project(normalize_remote(value['url']))

    def cli_checks(self):
        return [(['gh', 'api', '--help'], ['--hostname', '--paginate', '--slurp', '--method', '--field', '--raw-field']),
                (['gh', 'pr', 'view', '--help'], ['--json', '--repo']),
                (['gh', 'auth', 'status', '--help'], ['--hostname'])]

    def auth_check(self, project):
        return ['gh', 'auth', 'status', '--hostname', project.host]

    def account(self, project, cwd):
        raw = command(['gh', 'api', '--hostname', project.host, 'user'], cwd=cwd).stdout
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ApiError('authentication_failed', 'Invalid GitHub account response') from exc
        return self.account_from(project, value)

    def account_from(self, project, value):
        login = value.get('login') if isinstance(value, dict) else None
        if not isinstance(login, str) or not login:
            raise ApiError('authentication_failed', 'GitHub viewer login missing')
        return {'forge': 'github', 'host': project.host, 'username': login,
                'id': str(value['id']) if value.get('id') is not None else None}


from .forge_gitlab import GitLab

ADAPTERS = {"github": GitHub(), "gitlab": GitLab()}


def adapter(kind=None):
    kind = kind or "github"
    if kind not in ADAPTERS:
        raise ApiError('unsupported_forge', f'Unsupported forge: {kind}')
    return ADAPTERS[kind]


def require(kind, capability):
    if capability not in adapter(kind).capabilities:
        raise ApiError('unsupported_operation', f'This forge does not support {capability}.')


def restore(state, explicit=None):
    saved = state.get('forge_project')
    kind = saved.get('forge') if isinstance(saved, dict) else 'github'
    if explicit is not None and explicit != kind:
        raise ApiError('invalid_request', 'Forge conflicts with saved session.')
    project = adapter(kind).project(state['repository'])
    if saved is not None and saved != project.saved():
        raise ApiError('invalid_session', 'Saved forge identity conflicts with repository.')
    return project


def check_account(saved, current, username):
    if username and username.lower() != current['username'].lower():
        raise ApiError('account_changed', 'GitHub account changed; resume with the original account.')
    if saved and (saved.get('forge') != current['forge'] or saved.get('host') != current['host']
                  or (saved.get('id') is not None and saved['id'] != current.get('id'))):
        raise ApiError('account_changed', 'Authenticated forge account changed.')


def request_forge(request):
    """Select a saved forge without creating state or initializing a workflow."""
    from pathlib import Path
    from .common import read_json
    explicit = request.get('forge')
    adapter(explicit)
    if request.get('session'):
        path = Path(request['session'])
        if (path / 'session.json').is_file():
            state = read_json(path / 'session.json')
        elif (path / 'manifest.json').is_file():
            from .sessions import selected
            state = read_json(selected(path) / 'manifest.json')
        else:
            return explicit or 'github'  # Normal session validation supplies the error.
        return restore(state, explicit).forge
    return explicit or 'github'
