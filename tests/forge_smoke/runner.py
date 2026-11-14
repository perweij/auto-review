"""Durable live-test records and bounded subprocess execution."""
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import shlex
import subprocess
import tempfile
import tarfile
import time
import traceback


class Failure(RuntimeError):
    pass


class Blocked(Failure):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def snapshot(root):
    """Hash tracked and untracked source, without touching the real index."""
    output = subprocess.check_output(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=root)
    result = hashlib.sha256()
    for name in sorted(set(output.split(b'\0')) - {b''}):
        path = Path(root) / os.fsdecode(name)
        result.update(name + b'\0')
        if path.is_symlink():
            result.update(b'link\0' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            result.update(str(path.stat().st_mode & 0o777).encode() + b'\0' + path.read_bytes())
        else:
            result.update(b'deleted')
        result.update(b'\0')
    return result.hexdigest()


class Run:
    def __init__(self, root, directory=None, resume=False, verify_snapshot=True):
        self.deadline = None
        self.root = Path(root).resolve()
        if directory is None:
            parent = self.root / 'state/forge-smoke'
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = tempfile.mkdtemp(prefix='run-', dir=parent)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'manifest.json'
        identity = snapshot(self.root)
        if resume:
            self.data = json.loads(self.path.read_text())
            if verify_snapshot and self.data['snapshot'] != identity:
                raise Failure('Source changed since this run; start a fresh run.')
        else:
            if self.path.exists():
                raise Failure('Existing run; use --resume.')
            with tarfile.open(self.directory / 'source.tar.gz', 'x:gz') as archive:
                names = subprocess.check_output(['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=self.root)
                for name in sorted(set(names.split(b'\0')) - {b''}):
                    relative = os.fsdecode(name)
                    source = self.root / relative
                    if source.exists() or source.is_symlink():
                        archive.add(source, arcname=relative, recursive=False)
            revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.root, capture_output=True, text=True)
            self.data = dict(version=1, snapshot=identity, revision=revision.stdout.strip() if revision.returncode == 0 else None, created=time.time(), objects=[], results=[], commands=[])
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.directory / 'state'),
                        GIT_TERMINAL_PROMPT='0', GH_PROMPT_DISABLED='1', PYTHONDONTWRITEBYTECODE='1',
                        PR_REVIEW_MODEL_TIMEOUT='600')
        self.save()

    def trace_forges(self):
        directory = self.directory / 'trace-bin'
        directory.mkdir(exist_ok=True)
        config = {'log': str(self.directory / 'forge-calls.jsonl')}
        for tool in ('gh', 'glab'):
            executable = shutil.which(tool)
            if executable is None:
                raise Failure('Install ' + tool + ' before running smoke tests')
            config[tool] = executable
            link = directory / tool
            if not link.exists():
                link.symlink_to(self.root / 'tests/forge_smoke/trace.py')
        path = self.directory / 'trace.json'
        atomic_json(path, config)
        self.env.update(SMOKE_TRACE_CONFIG=str(path), PATH=str(directory)+os.pathsep+self.env['PATH'])

    def save(self):
        atomic_json(self.path, self.data)

    def command(self, args, cwd=None, input_text=None, timeout=120, env=None, check=True):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise Failure('Test phase reached its runtime limit; no further commands launched.')
            timeout = min(timeout, remaining)
        index = len(self.data['commands'])
        stdout = self.directory / ('command-%04d.stdout' % index)
        stderr = self.directory / ('command-%04d.stderr' % index)
        record = dict(argv=[str(a) for a in args], started=time.time(), stdout=stdout.name, stderr=stderr.name)
        self.data['commands'].append(record)
        self.save()
        with stdout.open('w') as out, stderr.open('w') as err:
            child_env = dict(env or self.env)
            child_env['SMOKE_ACTOR'] = 'application' if Path(str(args[0])).name in ('pr-review', 'fix-issue') else 'fixture'
            process = subprocess.Popen(args, cwd=cwd or self.root, env=child_env,
                                       stdin=subprocess.PIPE, stdout=out, stderr=err,
                                       text=True, start_new_session=True)
            try:
                process.communicate(input_text, timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                record['interrupted'] = True
                raise
            finally:
                record.update(exit_code=process.returncode, elapsed=time.time()-record['started'])
                self.save()
        output = stdout.read_text()
        if check and process.returncode:
            raise Failure('%s failed (%s); see %s' % (args[0], process.returncode, stderr))
        return process.returncode, output

    def api(self, request, cwd):
        _, output = self.command([str(self.root / 'bin/pr-review'), 'api'], cwd=cwd,
                                 input_text=json.dumps(dict(protocol=1, **request))+'\n', check=False)
        messages = [json.loads(line) for line in output.splitlines()]
        errors = [m for m in messages if m['type'] == 'error']
        if errors:
            raise Failure(json.dumps(errors[-1]['error']))
        return next(m['result'] for m in reversed(messages) if m['type'] == 'result')

    def scenario(self, name, function):
        dependencies = {
            'github-publication': ('github-review',), 'automatic-cli-json': ('github-review',), 'publication-recovery': ('github-publication',),
            'github-owner': ('github-publication',), 'owner-reply-recovery': ('github-owner',),
            'edited-preview': ('github-owner',), 'stale-head': ('github-review',),
            'interruption-and-concurrency': ('stale-head',), 'gitlab-advance': ('gitlab-review',),
            'github-issue': ('github-preflight',), 'foreign-issue-commit': ('github-issue',),
            'approval-changes': ('issue-plan-recovery-after',),
            'plan-receipt-before': ('issue-plan-recovery-before',),
            'plan-receipt-after': ('approval-changes',),
            'emacs-checkouts': ('interruption-and-concurrency', 'github-issue'),
            'accounting': ('github-review', 'gitlab-review', 'github-issue')}
        outcomes = {r['name']: r['status'] for r in self.data['results']}
        missing = [d for d in dependencies.get(name, ()) if outcomes.get(d) != 'passed']
        if missing:
            self.data['results'].append(dict(name=name, status='skipped', reason='Dependency did not pass: ' + ', '.join(missing)))
            self.save()
            print('SKIPPED', name, '(dependency)', flush=True)
            return False
        if not name.endswith('-preflight') and any(r['name'] == name and r['status'] == 'passed' for r in self.data['results']):
            print('REUSED ', name, flush=True)
            return True
        result = dict(name=name, status='running', started=time.time())
        self.data['results'].append(result)
        self.save()
        print('RUN    ', name, flush=True)
        try:
            function()
            result['status'] = 'passed'
        except Exception as exc:
            result.update(status='blocked' if name.endswith('-preflight') or isinstance(exc, Blocked) else 'failed', reason=str(exc))
            diagnostic = self.directory / (name + '-failure.txt')
            diagnostic.write_text(traceback.format_exc())
            result['diagnostic'] = diagnostic.name
        finally:
            if result['status'] == 'running':
                result.update(status='interrupted', reason='Interrupted before scenario completion')
            result['elapsed'] = time.time()-result['started']
            self.save()
        print(result['status'].upper(), name, result.get('reason', ''), flush=True)
        return result['status'] == 'passed'

    def report(self):
        atomic_json(self.directory / 'report.json', self.data)
        lines = ['# Forge smoke results', '', 'Artifacts: ' + str(self.directory),
                 'Mode: ' + ('real economical models' if self.data.get('suite') == 'live' else 'scripted models; real forge APIs and Git'), '',
                 'Source snapshot: ' + self.data['snapshot'], '']
        lines += ['- **%s** %s: %s' % (r['status'], r['name'], r.get('reason', '')) for r in self.data['results']]
        lines += ['', '## Retained fixtures', '']
        for kind, fixture in self.data.get('fixtures', {}).items():
            lines += ['- ' + kind + ': [' + fixture.get('url', 'pending') + '](' + fixture.get('url', '') + ')']
            if fixture.get('session'):
                lines += ['  Session: ' + fixture['session']]
            if fixture.get('checkout'):
                lines += ['  Checkout: ' + fixture['checkout']]
        issue = self.data.get('issue_fixture', {})
        if issue.get('url'):
            lines += ['- Issue: [' + issue['url'] + '](' + issue['url'] + ')']
        completed = issue.get('completed', {})
        if completed.get('pr_url'):
            lines += ['- Issue PR: [' + completed['pr_url'] + '](' + completed['pr_url'] + ')']
        if completed.get('worktree'):
            lines += ['  Owner checkout: ' + completed['worktree']]
        summaries = {kind: fixture.get('usage') for kind, fixture in self.data.get('fixtures', {}).items()}
        summaries['issue'] = (issue.get('completed') or issue.get('initial') or {}).get('usage')
        lines += ['', '## Usage', '', 'Authentication probes are included. Scripted calls consume no model quota; missing usage is not zero.',
                  'Quota deltas are account-wide observations, not attributable costs; do not sum them across sessions.']
        for label, summary in summaries.items():
            if not summary:
                continue
            reported = [r['usage']['total_tokens'] for r in summary.get('roles', []) if r.get('usage') is not None]
            tokens = str(sum(reported)) if reported else 'unavailable'
            lines += ['- ' + label + ': ' + str(summary['calls']) + ' calls; reported tokens ' + tokens + ' (coverage: ' + summary['token_coverage'] + ').']
            for quota in summary.get('quota', []):
                points = quota.get('consumed_points')
                lines += ['  ' + quota.get('label', quota['provider']) + ': ' + ('unavailable' if points is None else str(points) + ' percentage points observed') + '.']
        lines += ['', '## Assisted checks (pending)', '',
                  '- Open the retained review and Owner sessions in Emacs. Check menus, progress/attention, editing and w opening the expected Dired directory.',
                  '- Check finding elaboration interactively; this invokes its configured model.',
                  '- With a second account if available, check PR approval/request-changes and feedback from another author. Same-account issue approval is already covered.',
                  '', 'No merges or project deletion were performed. Fixtures remain until explicit cleanup.']
        (self.directory / 'report.md').write_text('\n'.join(lines)+'\n')
        latest = {r['name']: r['status'] for r in self.data['results']}
        print('Summary: ' + ', '.join(str(list(latest.values()).count(s)) + ' ' + s for s in ('passed', 'failed', 'blocked', 'skipped', 'interrupted')), flush=True)
        print('Results:', self.directory / 'report.md', flush=True)
        if self.data.get('suite') in ('smoke', 'extended', 'live'):
            print('Resume: python3 tests/smoke-forges.py --suite ' + self.data['suite'] + ' --resume ' + shlex.quote(str(self.directory)), flush=True)
        if self.data.get('objects'):
            print('Cleanup: python3 tests/smoke-forges.py --cleanup ' + shlex.quote(str(self.directory)), flush=True)
