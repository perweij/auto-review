"""Apply the locked Ruff formatter when a root CI step requires it."""
import os
from pathlib import Path
import re
import sys
import tempfile

from .common import ApiError
from .events import emit
from .isolation import _git, _safe_path
from .processes import run
from . import owner_git

STEP = re.compile(r'^ *(?:- +)?run: *([\x22\x27]?)uv run --no-sync ruff format --check \.\1 *(?:#.*)?$', re.M)


def root_ci_step(workflow):
    """Recognize literal steps in the supported block-style Actions layout."""
    # Match only scalar run steps inside jobs.<job>.steps. A workflow using
    # defaults with working-directory requires directory resolution beyond this adapter.
    if re.search(r'^ *defaults:', workflow, re.M) and 'working-directory' in workflow:
        return False
    parents = []
    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        value = line.lstrip(' ')
        if not value or value.startswith('#'):
            continue
        indent = len(line) - len(value)
        while parents and parents[-1][0] >= indent:
            parents.pop()
        keys = [key for _, key, _ in parents]
        if STEP.fullmatch(line) and keys[:1] == ['jobs'] and keys[2:3] == ['steps']:
            direct = len(keys) == 3 and value.startswith('- ')
            continuation = (len(keys) == 4 and keys[3].startswith('- ')
                            and indent == parents[-1][0] + 2 and not value.startswith('- '))
            if direct or continuation:
                start = index if direct else parents[-1][2]
                step_indent = indent if direct else parents[-1][0]
                step = []
                for row in lines[start:]:
                    if not row.strip() or row.lstrip().startswith('#'):
                        continue
                    if step and len(row) - len(row.lstrip()) <= step_indent:
                        break
                    step.append(row)
                if not any('working-directory:' in row for row in step):
                    return True
        parents.append((indent, value.rstrip(':'), index))
    return False


def version(root):
    workflows = root / '.github/workflows'
    if workflows.resolve() != workflows:
        return None
    for file in sorted(workflows.glob('*')):
        if file.suffix not in ('.yaml', '.yml') or file.is_symlink() or not file.is_file():
            continue
        try:
            workflow = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if root_ci_step(workflow):
            break
    else:
        return None
    lock = root / 'uv.lock'
    project = root / 'pyproject.toml'
    try:
        if not project.is_file() or project.is_symlink() or lock.is_symlink():
            raise ValueError('A regular root pyproject.toml and uv.lock are required')
        # uv emits one [[package]] table per locked distribution.
        tables = re.findall(r'^\[\[package\]\][ \t]*\n(.*?)' r'(?=^\[|\Z)', lock.read_text(), re.M | re.S)
        versions = set()
        for table in tables:
            if re.search(r'^name = "ruff"$', table, re.M):
                match = re.search(r'^version = "([^"]+)"$', table, re.M)
                versions.add(match.group(1) if match else None)
        if len(versions) != 1 or not re.fullmatch(r'\d+(?:\.\d+)+', next(iter(versions)) or ''):
            raise ValueError('Expected one pinned Ruff release in uv.lock')
        return versions.pop()
    except (KeyError, OSError, ValueError) as exc:
        raise ApiError('formatter_failed', f'Inspect the root Ruff configuration: {exc}') from exc


def check_tree(session, tree, invoke):
    """Run CI on tracked candidate content, excluding ignored local configuration."""
    with tempfile.TemporaryDirectory(prefix='ruff-check-', dir=session.path) as directory:
        checkout = Path(directory) / 'candidate'
        _git(directory, 'clone', '--shared', '--no-checkout', '--', str(session.repo.common), str(checkout))
        _git(checkout, 'read-tree', '--reset', '-u', tree)
        return invoke(['--check', '.'], checkout)


def gate(session, state, batch, before, snapshot, *, final=False):
    if batch.get('execution_kind') == 'issue_harness':
        return snapshot
    root = Path(state['worktree']).resolve()
    release = version(root)
    if not release:
        return snapshot

    def invoke(args, cwd=root):
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
        env['PATH'] = _safe_path(env.get('PATH', os.defpath))
        try:
            return run(['uv', '--no-config', 'tool', 'run', '--no-build', '--python', sys.executable, '--from', 'ruff==' + release,
                        'ruff', 'format', '--no-cache', *args], cwd=cwd, env=env,
                       timeout=180, stage='owner-ruff')
        except (OSError, ApiError) as exc:
            if isinstance(exc, ApiError) and exc.code != 'command_failed':
                raise
            raise ApiError('formatter_failed', f'Cannot run locked Ruff {release} with uv: {exc}') from exc

    def require_success(result):
        if result.reason or result.returncode:
            details = '\n'.join(s.strip() for s in (result.stdout, result.stderr, result.reason) if s)
            raise ApiError('formatter_failed', 'Inspect Ruff formatting before resuming: ' +
                           (details or f'Exit {result.returncode}'))

    def check_candidate():
        result = check_tree(session, snapshot['tree'], invoke)
        if owner_git.checkpoint(session, state, 'Ruff check')['tree'] != snapshot['tree']:
            raise ApiError('worktree_changed', 'Candidate changed during the Ruff check; inspect before resuming.')
        if not result.returncode and not result.reason:
            batch['ruff_check'] = dict(tree=snapshot['tree'], version=release)
        return result

    if final:
        check = check_candidate()
        if check.returncode == 0 and not check.reason:
            return snapshot
        if batch.get('verify_only') or check.returncode != 1 or check.reason:
            require_success(check)
    names = session.repo.git('diff', '--name-only', '--diff-filter=ACMRT', '-z',
                             before, snapshot['tree'], '--').stdout.split('\0')
    paths = [name for name in names if Path(name).suffix in ('.py', '.pyi', '.ipynb')
             and (root / name).is_file() and (root / name).resolve() == root / name]
    if final and not paths:
        require_success(check)
    if paths:
        emit('progress', message=f'Formatting {len(paths)} changed files with Ruff {release}')
        require_success(invoke(['--force-exclude', '--', *paths]))
        snapshot = owner_git.checkpoint(session, state, 'Ruff formatted candidate')
        batch['checkpoint'] = snapshot
    if final:
        require_success(check_candidate())
    return snapshot
