"""Run the batch GitLab Emacs workflow with real Git and local transport doubles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
from forge_smoke.gitlab_emacs import verify


def main():
    with tempfile.TemporaryDirectory(prefix='gitlab-emacs-') as directory:
        root = Path(directory)
        repo, remote, binary = root / 'checkout', root / 'remote.git', root / 'bin'
        binary.mkdir()
        env = dict(os.environ, FIXTURE=str(root), XDG_STATE_HOME=str(root / 'state'),
                   GIT_CONFIG_GLOBAL=str(root / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid',
                   MOCK_GLAB_WRITES='1', MOCK_GLAB_REPORTS='1',
                   PATH=str(binary) + os.pathsep + os.environ['PATH'],
                   SMOKE_CWD=str(repo), SMOKE_BACKEND=str(ROOT / 'bin/pr-review'),
                   SMOKE_MR_URL='https://gitlab.com/group/sub/repo/-/merge_requests/7',
                   SMOKE_EMACS_EVIDENCE=str(root / 'evidence.json'))
        def git(*args, cwd=None):
            return subprocess.check_output(['git', *map(str, args)], cwd=cwd, env=env, text=True, stderr=subprocess.PIPE).strip()
        git('init', '-q', '--bare', remote)
        git('init', '-q', '-b', 'main', repo)
        source = 'def total(price, quantity):\n    """Total cost of quantity items."""\n    return price * quantity\n'
        (repo / 'price.py').write_text(source)
        git('add', '.', cwd=repo)
        git('commit', '-qm', 'base', cwd=repo)
        base = git('rev-parse', 'HEAD', cwd=repo)
        (repo / 'price.py').write_text(source.replace('price * quantity', 'price + quantity'))
        git('commit', '-qam', 'regression', cwd=repo)
        git('push', '-q', remote, base + ':refs/heads/main', 'HEAD:refs/merge-requests/7/head', cwd=repo)
        git('remote', 'add', 'origin', 'https://gitlab.com/group/sub/repo.git', cwd=repo)
        git('config', '--global', 'url.' + str(remote) + '.insteadOf', 'https://gitlab.com/group/sub/repo.git')
        for tool in ('claude', 'codex'):
            (binary / tool).symlink_to(ROOT / 'tests/helpers/mock-cli')
        (binary / 'glab').symlink_to(ROOT / 'tests/helpers/mock-glab.py')
        (binary / 'gh').write_text('#!/bin/sh\nexit 99\n')
        (binary / 'gh').chmod(0o755)
        for name in ('mock-bridge-request', 'mock-bridge-response'):
            os.mkfifo(root / name, 0o600)
        (root / 'findings.json').write_text(json.dumps([dict(id='F1', title='Arithmetic', severity='Medium',
            validity='CONFIRMED', blocking=False, evidence='price.py:3 adds instead of multiplying.',
            body_md='Multiply price by quantity.', primary_location=dict(path='price.py', side='RIGHT', line=3, start_line=3), locations=[])]))
        bridge = subprocess.Popen([sys.executable, str(ROOT / 'tests/helpers/mock-review-bridge.py'), str(root)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            subprocess.run([*sys.argv[1:], '-l', str(ROOT / 'tests/forge_smoke/gitlab-emacs.el')],
                           cwd=ROOT, env=env, check=True, timeout=300)
            evidence = json.loads((root / 'evidence.json').read_text())
            verify(evidence, json.loads((root / 'gitlab-notes.json').read_text()))
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            if any(c['tool'] == 'gh' for c in calls):
                raise AssertionError('GitLab Emacs invoked gh')
            print('GitLab Emacs real-process workflow passed with local forge/model doubles.')
        finally:
            bridge.terminate()
            bridge.wait(timeout=5)


if __name__ == '__main__':
    main()
