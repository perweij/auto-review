"""Local, read-only review target resolution. No forge CLI or preference writes."""
from .common import ApiError, git, read_json, state_root
from .forge import adapter, restore


def project_for(repository, forge=None):
    """Classify the public GitLab host, preserving other GitHub hosts."""
    if not isinstance(repository, str) or not repository:
        raise ApiError('invalid_request', 'Repository must be a project path, optionally prefixed by its host.')
    host = repository.split('/', 1)[0].lower()
    inferred = 'gitlab' if host == 'gitlab.com' else None
    if inferred and forge and forge != inferred:
        raise ApiError('invalid_request', 'Forge conflicts with repository host.')
    return adapter(forge or inferred).project(repository)


def target(value, repository=None, forge=None):
    kind = forge
    if str(value).lower().startswith('https://gitlab.com/'):
        if kind and kind != 'gitlab':
            raise ApiError('invalid_request', 'Forge conflicts with MR URL.')
        kind = 'gitlab'
    project = project_for(repository, kind) if repository else None
    implied, number = adapter(kind or (project.forge if project else None)).target(value)
    if project and implied and project != implied:
        raise ApiError('invalid_request', 'Review URL conflicts with repository.')
    return project or implied, number


def manifests(common):
    """Retained review and Owner roots; archives never nominate another project."""
    files = set(state_root().glob('*/*/*/*/manifest.json')) | set(state_root().glob('gitlab/*/*/manifest.json'))
    files.update((state_root() / 'owners-v1').glob('*/pr-*/session.json'))
    for file in sorted(files):
        try:
            value = read_json(file, {})
            if isinstance(value, dict) and value.get('git_common_dir') == common:
                yield file, value, restore(value)
        except (ApiError, KeyError, TypeError, ValueError):
            continue


def resolve(request):
    from .forge import capabilities
    cwd = request.get('cwd', '.')
    common = git(['rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=cwd).stdout.strip()
    explicit = request.get('forge')
    adapter(explicit)
    if request.get('pr') is not None:
        project, number = target(request['pr'], request.get('repository'), explicit)
    else:
        project = project_for(request['repository'], explicit) if request.get('repository') else None
        number = None
    remotes = set()
    from .repository import normalize_remote
    for line in git(['config', '--get-regexp', r'^remote\..*\.url$'], cwd=cwd, check=False).stdout.splitlines():
        parts = line.split(None, 1)
        name = normalize_remote(parts[1]) if len(parts) == 2 else ''
        if not name:
            continue
        try:
            candidate = project_for(name)
        except ApiError:
            continue
        if not explicit or candidate.forge == explicit:
            remotes.add(candidate)
    saved = {}
    for file, value, candidate in manifests(common):
        if not explicit or candidate.forge == explicit:
            saved.setdefault(candidate, []).append({'session': str(file.parent), 'pr': value.get('pr')})
    # Sessions annotate competing remotes; they cannot silently choose one.
    candidates = remotes or set(saved)
    if project:
        candidates.add(project)
    elif len(candidates) == 1:
        project = next(iter(candidates))
    rows = [dict(forge_project=p.saved(), repository=p.repository, sessions=saved.get(p, []))
            for p in sorted(candidates, key=lambda p: (p.forge, p.repository))]
    return dict(forge_project=project.saved() if project else None,
                repository=project.repository if project else None, pr=number,
                url=adapter(project.forge).url(project, number) if project and number else None,
                capabilities=capabilities(project.forge) if project else None, projects=rows)
