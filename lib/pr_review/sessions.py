"""Durable review generations and resumable, candidate-only replacement journals."""
import copy
from pathlib import Path
import shutil
import tempfile

from .common import ApiError, locked, now, read_json, state_root
from .events import emit
from .storage import hash_bytes, hash_file, pointer, write_json, sync_directory

DIRECTORIES = ('context', 'prompts', 'reviews', 'publication', 'logs', 'discussion', 'suggestions')


def say(message):
    emit('progress', message=message)


def selected(root):
    for name in ('.pending-review', '.current-review'):
        link = root / name
        if link.is_symlink():
            path = link.resolve(strict=True)
            if not (path / 'manifest.json').is_file():
                raise ApiError('invalid_session', 'Saved review attempt is missing')
            return path
    return root


def locate(repo, resume=None):
    main = Path(repo.git('worktree', 'list', '--porcelain').stdout.splitlines()[0].removeprefix('worktree '))
    worktree = main.parent / (repo.name.split('/')[-1] + f'-review-pr-{repo.pr}')
    if resume:
        root = Path(resume).resolve(strict=True)
        manifest = read_json(root / 'manifest.json', {})
        root = Path(manifest.get('session_directory', root)).resolve()
        return root, Path(read_json(selected(root) / 'manifest.json', {})['worktree'])
    parent = state_root() / repo.identity
    matches = []
    existing = worktree.exists() or worktree.is_symlink()
    head = None
    if existing:
        result = repo.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=worktree, check=False)
        if result.returncode:
            raise ApiError('invalid_session', f'Existing path is not a Git worktree: {worktree}')
        common = result.stdout.strip()
        top = repo.git('rev-parse', '--show-toplevel', cwd=worktree).stdout.strip()
        if common != str(repo.common) or top != str(worktree):
            raise ApiError('invalid_session', f'Existing worktree belongs to a different checkout: {worktree}')
        head = repo.git('rev-parse', 'HEAD', cwd=worktree).stdout.strip()
    for file in parent.glob('*/manifest.json'):
        try:
            m = read_json(selected(file.parent) / 'manifest.json', {})
        except (ApiError, OSError):
            continue  # Unrelated damaged sessions do not hide the matching run.
        if not isinstance(m, dict):
            continue
        if (m.get('repository') == repo.identity and str(m.get('pr')) == str(repo.pr)
                and m.get('git_common_dir') == str(repo.common) and m.get('worktree') == str(worktree)
                and ((existing and m.get('format_version') == 2 and (m.get('snapshot', {}).get('head', head) == head or m.get('snapshot_update', {}).get('to', {}).get('head') == head))
                     or not existing and m.get('snapshot_update'))):
            matches.append(file.parent)
    if len(matches) > 1:
        raise ApiError('ambiguous_session', 'Multiple saved runs match this worktree; select one with --resume RUN_DIRECTORY:\n' + '\n'.join(map(str, matches)))
    if matches:
        say(f'Automatically resuming PR #{repo.pr} from {matches[0]}')
        return matches[0], worktree
    if existing:
        raise ApiError('invalid_session', f'Existing worktree has no matching saved run under {parent}. Use --resume with its original run directory: {worktree}')
    top = Path(repo.git('rev-parse', '--show-toplevel').stdout.strip())
    if any(parent.is_relative_to(p) for p in (main, top, worktree)):
        raise ApiError('invalid_session', 'Artifacts must be outside the reviewed repository; set XDG_STATE_HOME')
    return None, worktree


class Store:
    def __init__(self, repo, root, worktree):
        self.repo, self.worktree = repo, worktree
        self.resuming = root is not None
        if root is None:
            parent = state_root() / repo.identity
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            root = Path(tempfile.mkdtemp(prefix=f'pr-{repo.pr}-', dir=parent))
            self.manifest = dict(format_version=2, repository=repo.identity, pr=repo.pr, url=repo.url,
                git_common_dir=str(repo.common), worktree=str(worktree), run_id=root.name, created_at=now(),
                status='running', phases={}, publication={'status': 'not_published'}, warnings=[], generated_files=[], removed_environment={}, draft_mutations_tracked=True)
            self.root = self.path = root
            self.reset_directories(DIRECTORIES)
            self.save()
        else:
            self.root, self.path = root, selected(root)
            self.manifest = read_json(self.path / 'manifest.json', {})
            if self.manifest.get('format_version') != 2:
                raise ApiError('invalid_session', 'Legacy run remains readable but cannot resume discussion-aware publication; start a new run after moving its worktree.')
            if (self.manifest.get('repository') != repo.identity or str(self.manifest.get('pr')) != str(repo.pr)
                    or self.manifest.get('git_common_dir') != str(repo.common)):
                raise ApiError('invalid_session', 'Resume repository or PR identity does not match')
            if (root / '.pending-review').is_symlink() and (root / '.current-review').is_symlink() and self.current == self.path:
                self.clear_pending()
            for path in {self.path, self.current}:
                state = read_json(path / 'publication/drafts.json', {})
                from .receipts import actions as saved_actions
                actions = saved_actions(path, include_copied=True)
                if (any(a.get('status') in ('submitting', 'ambiguous') for b in state.get('batches', []) for a in b['actions'])
                        or any(a.get('status') not in ('draft', 'published', 'superseded') for a in actions)):
                    raise ApiError('uncertain_publication', 'Resolve uncertain publication in the Auto-review publication menu before running or replacing this review', session=str(self.root))
            (self.path / 'logs').mkdir(exist_ok=True, mode=0o700)
            if self.manifest.get('publication_reset', {}).get('status') == 'pending':
                self.finish_publication_reset()
        self.original_manifest = copy.deepcopy(self.manifest)
        self.original_prompts = {p.name.removesuffix('-prompt.txt'): p.read_bytes() for p in (self.path / 'prompts').glob('*-prompt.txt')}
        from .automation import observe
        observe(self)

    @property
    def current(self):
        link = self.root / '.current-review'
        return link.resolve() if link.is_symlink() else self.root

    @property
    def pending(self):
        link = self.root / '.pending-review'
        return link.resolve() if link.is_symlink() else None

    def save(self):
        self.manifest['updated_at'] = now()
        write_json(self.path / 'manifest.json', self.manifest)
        from .automation import observe
        observe(self)

    def reload(self):
        self.manifest = read_json(self.path / 'manifest.json', {})

    def clear_pending(self):
        (self.root / '.pending-review').unlink(missing_ok=True)
        sync_directory(self.root)

    def reset_directories(self, names):
        for name in names:
            directory = self.path / name
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(mode=0o700)
        if 'logs' in names:
            write_json(self.path / 'logs/commands.json', [])
        if 'publication' in names:
            write_json(self.path / 'publication/actions.json', [])

    def pin(self, path, snapshot):
        identity = hash_bytes(str(path).encode())[:24]
        for key, suffix in (('head', 'head'), ('merge_base', 'merge-base')):
            if snapshot.get(key):
                self.repo.git('update-ref', f'refs/pr-review/{self.repo.pr}/history/{identity}/{suffix}', snapshot[key])

    def begin(self):
        if self.pending == self.path:
            return
        previous = self.path
        # Copy under the same lock as draft saves: no half-written ledger, and
        # the last human edit stays attached to the generation it belongs to.
        with locked(previous / 'publication/.drafts.lock', wait=True):
            from .automation import guard_replacement
            guard_replacement(self)
            self.reload()
            self.pin(previous, self.manifest.get('snapshot', {}))
            history = Path(str(self.root) + '-history')
            history.mkdir(exist_ok=True, mode=0o700)
            candidate = Path(tempfile.mkdtemp(prefix='attempt-', dir=history))
            for name in DIRECTORIES:
                source = previous / name
                if source.is_dir():
                    shutil.copytree(source, candidate / name)
                else:
                    (candidate / name).mkdir()
            self.path = candidate
            self.manifest.update(session_directory=str(self.root), attempt=dict(status='building', previous=str(previous)), draft_mutations_tracked=True)
            self.manifest.pop('publication_reset', None)
            for obj, key in ((self.manifest.get('discussion', {}), 'path'), (self.manifest.get('reassessment', {}), 'input'), (self.manifest.get('reassessment', {}), 'discussion')):
                if isinstance(obj.get(key), str) and obj[key].startswith(str(previous) + '/'):
                    obj[key] = str(candidate) + obj[key][len(str(previous)):]
            reassessment = read_json(candidate / 'reviews/reassessment-input.json')
            if reassessment and isinstance(reassessment.get('discussion'), str):
                reassessment['discussion'] = reassessment['discussion'].replace(str(previous) + '/', str(candidate) + '/', 1)
                write_json(candidate / 'reviews/reassessment-input.json', reassessment)
            self.save()
            pointer(self.root, '.pending-review', candidate)
        say(f'Building replacement review in: {candidate}\nPrevious artifacts remain available: {previous}')

    def reset_publication(self):
        if self.pending == self.path and self.manifest.get('publication_reset', {}).get('status') == 'complete':
            return
        from .receipts import actions as saved_actions
        actions = saved_actions(self.path, include_copied=True)
        if not isinstance(actions, list) or any(not isinstance(a, dict) or not isinstance(a.get('status'), str) for a in actions):
            raise ApiError('invalid_artifact', 'Cannot verify saved publication history; replacement stopped without discarding receipts')
        if not any(a['status'] not in ('draft', 'superseded') for a in actions):
            return
        if any(a['status'] not in ('draft', 'published', 'superseded') for a in actions):
            raise ApiError('uncertain_publication', 'Resolve uncertain publication before replacing its analysis')
        self.begin()
        self.manifest['publication_reset'] = dict(status='pending', archive=self.manifest['attempt']['previous'], run_id=self.manifest['run_id'] + '-' + self.path.name)
        self.save()
        self.finish_publication_reset()

    def finish_publication_reset(self):
        journal = self.manifest['publication_reset']
        archive = Path(journal['archive'])
        if archive == self.path or not (archive / 'publication/actions.json').is_file():
            raise ApiError('invalid_artifact', 'Previous publication history is missing; replacement stopped without discarding receipts')
        self.reset_directories(('publication',))
        self.manifest.update(publication=dict(status='not_published', published_actions=0), run_id=journal['run_id'],
                             previous_review=dict(archive=str(archive), worktree=str(self.worktree)))
        journal['status'] = 'complete'
        self.manifest['phases'].pop('contributions', None)
        self.save()

    def scope(self, kind, threads):
        if kind == 'followup' and not self.resuming:
            raise ApiError('invalid_request', 'Follow-up requires an existing saved review')
        if self.manifest.get('analysis_kind', 'full') == kind and (kind != 'followup' or self.manifest.get('followup_threads', []) == threads):
            return
        if self.pending:
            self.clear_pending()
        self.begin()
        self.reset_directories(('reviews', 'publication', 'prompts'))
        for name in ('claude', 'codex', 'synthesis', 'contributions', 'followup'):
            self.manifest['phases'].pop(name, None)
        for name in ('reassessment', 'publication_reset'):
            self.manifest.pop(name, None)
        self.manifest.update(analysis_kind=kind, followup_threads=threads, publication=dict(status='not_published'))
        self.save()

    def update_snapshot(self, snapshot):
        old = self.manifest['snapshot']
        if (self.repo.git('rev-parse', 'HEAD', cwd=self.worktree).stdout.strip() != old['head']
                or self.repo.git('diff', '--quiet', 'HEAD', '--', cwd=self.worktree, check=False).returncode
                or self.repo.git('diff', '--cached', '--quiet', '--', cwd=self.worktree, check=False).returncode):
            raise ApiError('integrity_failed', 'Saved review worktree has tracked changes or changed HEAD; preserve or restore them before updating')
        self.begin()
        say('PR snapshot changed. Updating this review.')
        history = Path(str(self.path) + '-history')
        history.mkdir(exist_ok=True, mode=0o700)
        archive = Path(tempfile.mkdtemp(prefix='snapshot-', dir=history))
        shutil.copytree(self.path, archive, dirs_exist_ok=True)
        self.manifest['snapshot_update'] = dict(archive=str(archive), worktree=str(self.worktree) + '-' + archive.name,
            run_id=self.path.name + '-' + archive.name, **{'from': old, 'to': snapshot})
        self.save()
        self.finish_snapshot_update()

    def finish_snapshot_update(self):
        journal = self.manifest['snapshot_update']
        archive, saved = Path(journal['archive']), Path(journal['worktree'])
        if not (archive / 'manifest.json').is_file():
            raise ApiError('invalid_artifact', 'Snapshot archive missing; cannot update safely')
        self.pin(archive, journal['from'])
        if not saved.exists():
            if (self.repo.git('rev-parse', 'HEAD', cwd=self.worktree).stdout.strip() != journal['from']['head']
                    or self.repo.git('diff', '--quiet', 'HEAD', '--', cwd=self.worktree, check=False).returncode
                    or self.repo.git('diff', '--cached', '--quiet', '--', cwd=self.worktree, check=False).returncode):
                raise ApiError('integrity_failed', 'Previous worktree changed during update')
            self.repo.git('worktree', 'move', '--', str(self.worktree), str(saved))
        for directory, head in ((saved, journal['from']['head']), (self.worktree, journal['to']['head'])):
            if directory == self.worktree and not directory.exists():
                self.repo.git('worktree', 'add', '--detach', str(directory), head)
            if (self.repo.git('rev-parse', 'HEAD', cwd=directory).stdout.strip() != head
                    or self.repo.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=directory).stdout.strip() != str(self.repo.common)):
                raise ApiError('integrity_failed', 'Archived or updated worktree identity mismatch')
        self.reset_directories(DIRECTORIES)
        self.manifest.setdefault('snapshot_history', []).append(dict(archive=str(archive), worktree=str(saved), snapshot=journal['from']))
        self.manifest.update(previous_review=dict(archive=str(archive), worktree=str(saved)), snapshot=journal['to'], run_id=journal['run_id'],
                             phases={}, publication=dict(status='not_published'), generated_files=[], warnings=[])
        for key in ('snapshot_update', 'reassessment', 'review_restart', 'agent_config_confirmed', 'integrity_failed', 'publication_reset'):
            self.manifest.pop(key, None)
        self.save()
        say(f'Previous review and drafts preserved: {archive}\nPrevious worktree preserved: {saved}\nReviewing the updated snapshot from scratch.')

    def promote(self, stages):
        if self.pending != self.path:
            return
        for stage in stages:
            name = {'claude': 'claude-independent.md', 'codex': 'codex.md'}.get(stage, stage + '.json')
            path = self.path / 'reviews' / name
            record = self.manifest['phases'].get(stage, {})
            if record.get('status') != 'completed' or not path.is_file() or record.get('output_hash') != hash_file(path):
                raise ApiError('incomplete_review', 'Replacement review is incomplete or changed; previous review remains current')
        with locked(self.current / 'publication/.drafts.lock', wait=True):
            from .automation import guard_replacement
            guard_replacement(self)
            self.manifest['attempt']['status'] = 'ready'
            self.save()
            pointer(self.root, '.current-review', self.path)
            self.clear_pending()
        say('Replacement review and drafts are ready; made them current.')
