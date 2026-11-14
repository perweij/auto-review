"""Separate reviewer workspaces inside the caller-provided sandbox container."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tempfile

from .common import ApiError, state_root
from .events import emit
from .processes import run as process
from .storage import write_json


def _safe_path(value):
    return os.pathsep.join(part for part in value.split(os.pathsep) if part and os.path.isabs(part))


def _executable(name, path):
    executable = shutil.which(name, path=_safe_path(path))
    if not executable or not os.path.isabs(executable):
        raise ApiError('independence_failed', f'Cannot locate trusted executable {name}')
    return executable


def _git(cwd, *args, check=True):
    # The clone is disposable; do not execute host-configured checkout filters
    # while constructing it from an untrusted PR snapshot.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    env['PATH'] = _safe_path(env.get('PATH', ''))
    result = process([_executable('git', env['PATH']), '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', *args], cwd=cwd, env=env)
    if check and result.returncode:
        raise ApiError('independence_failed', 'Could not prepare or verify the isolated Git snapshot: ' + result.stderr.strip())
    return result


def prepare_checkout(source, head_ref, base_ref, head, base, destination, cwd):
    """Create a disposable detached checkout and return its tracked paths.

    The clone is disposable and never shares objects with its source. Refs named
    by the caller are fetched, ``head`` is checked out detached and ``base_ref``
    is pinned to ``base``. It serves independent reviewers and direct stages.
    """
    _git(cwd, 'clone', '--no-local', '--no-checkout', '--', str(source), str(destination))
    # PR refs are outside refs/heads and are not included by clone's
    # default refspec, especially for fork pull requests.
    _git(destination, 'fetch', '--no-tags', 'origin', head_ref, base_ref)
    _git(destination, 'checkout', '--detach', head)
    _git(destination, 'update-ref', base_ref, base)
    return set(_git(destination, 'ls-files', '-z').stdout.split('\0'))


def preserve_generated(checkout, tracked, logs, run_path, stage):
    """Retain untracked files without placing them in another reviewer's tree."""
    checkout, archive = Path(checkout), None
    retained, links_saved = [], {}
    for directory, folders, files in os.walk(checkout, followlinks=False):
        here = Path(directory)
        if here == checkout:
            folders[:] = [name for name in folders if name != '.git']
        links = [name for name in folders if (here / name).is_symlink()]
        folders[:] = [name for name in folders if name not in links]
        for name in files + links:
            source = here / name
            relative = source.relative_to(checkout)
            if str(relative) in tracked:
                continue
            if not (source.is_file() or source.is_symlink()):
                continue
            if archive is None:
                logs, run_path = Path(logs), Path(run_path)
                base = logs / 'generated'
                if logs.is_symlink() or logs.resolve() != run_path.resolve() / 'logs' or base.is_symlink():
                    raise ApiError('invalid_artifact', 'Invalid generated-file preservation directory')
                base.mkdir(exist_ok=True, mode=0o700)
                archive = Path(tempfile.mkdtemp(prefix=stage + '-', dir=base))
            if source.is_symlink():
                links_saved[str(relative)] = os.readlink(source)
            else:
                destination = archive / 'files' / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(source, destination)
            retained.append(str(relative))
    if links_saved:
        write_json(archive / 'symlinks.json', links_saved)
    return archive, retained


@dataclass
class WorkspaceInputs:
    """Everything a reviewer workspace needs from its workflow, without PR attributes."""
    source: Path            # repository whose objects feed the disposable clone
    head_ref: str
    base_ref: str
    head: str
    base: str
    scratch: Path           # parent of the private root
    cwd: Path               # host-side working directory; never the candidate itself
    output: Path            # run directory receiving ``reviews/<stage>.md.tmp`` and generated archives
    protected: list = field(default_factory=list)  # exclude workflow state from copied provider config
    context: dict = field(default_factory=dict)   # private context files: relative name -> bytes


def pr_inputs(run):
    """Describe a PR review session. Its attributes remain the PR contract."""
    protected = [state_root(), run.root.resolve(), run.path.resolve(), run.worktree.resolve()]
    for path in (getattr(run, 'current', None), getattr(run, 'pending', None)):
        if path is not None:
            protected.append(Path(path).resolve())
    history_paths = [str(run.root) + '-history', str(run.path) + '-history',
                     run.manifest.get('attempt', {}).get('previous'),
                     run.manifest.get('previous_review', {}).get('archive')]
    for snapshot in run.manifest.get('snapshot_history', []):
        history_paths.extend((snapshot.get('archive'), snapshot.get('worktree')))
    protected.extend(Path(value).resolve() for value in history_paths if value and Path(value).is_dir())
    snapshot = run.manifest['snapshot']
    return WorkspaceInputs(source=run.repo.common, head_ref=run.repo.head_ref, base_ref=run.repo.base_ref,
                         head=snapshot['head'], base=snapshot['base'], scratch=run.scratch, cwd=run.worktree,
                         output=run.path, protected=protected)


class IndependentWorkspace:
    """A disposable clone and fresh provider home inside the outer container.

    This separates inputs and sessions, not filesystem access. The enclosing
    container supplies the security boundary; peers are not deliberately given
    each other's reports or session state.
    """
    def __init__(self, run, stage, provider, inputs=None):
        self.temp = None
        try:
            self._initialize(run, stage, provider, inputs)
        except BaseException as exc:
            if self.temp is not None:
                self.close()
            if isinstance(exc, OSError):
                raise ApiError("independence_failed", f"Cannot prepare reviewer workspace: {exc}") from exc
            raise

    def _initialize(self, run, stage, provider, inputs):
        self.run, self.stage, self.provider = run, stage, provider
        self.inputs = inputs if inputs is not None else pr_inputs(run)
        _executable('git', os.environ.get('PATH', ''))
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-review-', dir=self.inputs.scratch)
        self.root = Path(self.temp.name)
        self.checkout = self.root / 'checkout'
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        (self.root / 'tmp').mkdir(mode=0o700)
        (self.root / 'runtime').mkdir(mode=0o700)
        for name in ('.cache', '.config', '.local/state'):
            (self.home / name).mkdir(mode=0o700, parents=True)
        inputs = self.inputs
        self.tracked = prepare_checkout(inputs.source, inputs.head_ref, inputs.base_ref, inputs.head,
                                        inputs.base, self.checkout, inputs.cwd)
        self.verifier = self.root / 'verification.git'
        shutil.copytree(self.checkout / '.git', self.verifier)
        # Specification and feedback reach a reviewer only through files in its
        # private root; no prompt argument exists for ``codex exec review --base``.
        for name, content in inputs.context.items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ApiError("independence_failed", "Invalid reviewer context path")
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(content)
        self.protected = list(inputs.protected)
        home = Path.home()
        source_home = Path(os.environ.get('CODEX_HOME', home / '.codex')) if provider == 'codex' else Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude'))
        files = [(source_home / 'auth.json', '.codex/auth.json'),
                 (source_home / 'config.toml', '.codex/config.toml')] if provider == 'codex' else [
                 (source_home / '.credentials.json', '.claude/.credentials.json'),
                 (source_home / 'settings.json', '.claude/settings.json'),
                 (home / '.claude.json', '.claude.json')]
        for source, name in files:
            if source.is_file():
                source = source.resolve()
                if any(source.is_relative_to(path) for path in self.protected):
                    raise ApiError('independence_failed', 'Provider configuration overlaps protected review files')
                dest = self.home / name
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, dest)
                dest.chmod(0o600)
        directories = [(source_home / 'skills', '.codex/skills')] if provider == 'codex' else [
            (source_home / 'skills', '.claude/skills'), (source_home / 'commands', '.claude/commands'),
            (source_home / 'plugins', '.claude/plugins')]
        xdg_config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        directories.append((xdg_config / provider, '.config/' + provider))
        for source, target in directories:
            if source.is_dir():
                source = source.resolve()
                if any(source.is_relative_to(path) or path.is_relative_to(source) for path in self.protected):
                    raise ApiError('independence_failed', 'Provider configuration overlaps protected review files')
                dest = self.home / target
                dest.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copytree(source, dest, dirs_exist_ok=True)

    def wrap(self, argv, env, *, provider_home=None):
        safe_path = _safe_path(env.get('PATH', ''))
        env = {key: value for key, value in env.items() if key.upper() not in {
            'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
            'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE',
            'GIT_NAMESPACE', 'GIT_CEILING_DIRECTORIES', 'GIT_CONFIG_SYSTEM', 'GIT_EXTERNAL_DIFF',
            'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'}
            and not key.upper().startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))}
        # Persistent conversations keep provider-owned indexes at their original
        # absolute paths; source checkouts and runtime files remain disposable.
        home = Path(provider_home) if provider_home is not None else self.home
        env = dict(env, HOME=str(home), TMPDIR=str(self.root / 'tmp'),
                   CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'),
                   XDG_CACHE_HOME=str(home / '.cache'), XDG_CONFIG_HOME=str(home / '.config'),
                   XDG_STATE_HOME=str(home / '.local/state'), XDG_RUNTIME_DIR=str(self.root / 'runtime'),
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', PATH=safe_path)
        output = str(self.inputs.output / f'reviews/{self.stage}.md.tmp')
        command = [_executable(argv[0], safe_path), *argv[1:]]
        return [(str(self.root / 'result.md') if arg == output else arg) for arg in command], env

    def verify(self):
        gitdir = self.checkout / '.git'
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise ApiError('integrity_failed', 'Independent reviewer changed its isolated Git directory')
        # Verify content using a saved Git directory and fresh index, never the
        # reviewer's index, filters, hooks or config. These are integrity guards,
        # not protection from a process deliberately attacking the controller.
        prefix = ['--git-dir=' + str(self.verifier), '--work-tree=' + str(self.checkout)]
        def invoke(*args):
            return _git(self.checkout, *prefix, *args)
        head = _git(self.checkout, 'rev-parse', 'HEAD')
        base = _git(self.checkout, 'rev-parse', self.inputs.base_ref)
        (self.verifier / 'index').unlink(missing_ok=True)
        invoke('read-tree', self.inputs.head)
        invoke('add', '-u', '--', '.')
        staged = _git(self.checkout, 'write-tree').stdout.strip()
        tree = invoke('write-tree').stdout.strip()
        expected = invoke('rev-parse', self.inputs.head + '^{tree}').stdout.strip()
        if (head.stdout.strip() != self.inputs.head or base.stdout.strip() != self.inputs.base or tree != expected or staged != expected):
            raise ApiError('integrity_failed', 'Independent reviewer changed its isolated snapshot')

    def preserve_generated(self):
        """Retain untracked files without placing them in another reviewer's tree."""
        return preserve_generated(self.checkout, self.tracked, self.inputs.output / 'logs',
                                  self.inputs.output, self.stage)

    def close(self):
        try:
            self.temp.cleanup()
        except OSError as exc:
            # Container tools can leave storage owned by mapped user IDs. A
            # disposal failure must not discard verified output or mask errors.
            emit('progress', message=f'Warning: Could not remove temporary reviewer workspace '
                 f'{self.temp.name}: {exc}. Leftover files require manual cleanup.')
