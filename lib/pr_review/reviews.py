"""Shared review service. Terminal and API invoke this directly, without a CLI subprocess."""
import json
from pathlib import Path
import re
import shutil
import tempfile

from .common import ApiError, command, locked, now, read_json
from . import cache, providers, settings
from .processes import Cancelled, cancellation_scope, run as process
from .repository import Repository
from .sessions import Store, locate, say
from .storage import atomic_bytes, hash_file, write_json


class Review(Store):
    def __init__(self, repo, root, worktree, configuration, runtime, tools, viewer, permission):
        super().__init__(repo, root, worktree)
        self.settings, self.runtime, self.tools = configuration, runtime, tools
        self.viewer, self.permission = viewer, permission
        self.scratch = None
        self.active = None

    @property
    def dual(self):
        return self.settings['reviewers'] == ['claude', 'codex']

    def command(self, stage):
        return providers.review_command(self.settings, self.runtime, stage, self.path, self.repo.base_ref)

    def configure(self, kind, threads):
        self.scope(kind, threads)
        if (settings.from_manifest(self.manifest) != self.settings and
                any(self.manifest['phases'].get(s, {}).get('status') == 'completed' for s in ('synthesis', 'followup'))):
            self.begin()
        self.manifest.update(configuration=self.settings, tools=self.tools,
            models=dict(codex=self.settings['codex_model'], claude=self.settings['claude_model'],
                        claude_effort=self.settings['claude_effort'], codex_effort=self.settings['codex_effort'], claude_permission_mode=self.runtime.permission),
            viewer=self.viewer, viewer_permission=self.permission, status='running')
        self.save()
        say(f'Artifacts: {self.path}')

    def model(self, stage, argv, input_bytes=b'', probe=False):
        provider = stage.split('_')[0] if probe else providers.provider_for(self.settings, stage)
        model = ('haiku' if provider == 'claude' else self.runtime.probe_model) if probe else self.settings[provider + '_model']
        output = self.path / ('reviews/claude-independent.jsonl' if stage == 'claude' else
                              'logs/codex.events.jsonl' if stage == 'codex' else
                              f'logs/{stage}.jsonl' if probe else f'logs/{stage}.raw.json')
        stderr = self.path / ('logs/claude-independent.stderr.log' if stage == 'claude' else f'logs/{stage}.stderr.log')
        env, removed = providers.environment(provider)
        self.manifest.setdefault('removed_environment', {})[provider] = removed
        record = dict(status='running', fingerprint='', started_at=now(), stdout_log=str(output), stderr_log=str(stderr), model=model)
        if not probe:
            record['inputs'] = cache.inputs(self, stage)
            record['fingerprint'] = cache.fingerprint(record['inputs'])
            if stage != 'contributions':
                record['testing_policy'] = self.settings['synthesis_tests'] if stage == 'synthesis' else 'focused'
                say('Verification: ' + ('one full-suite attempt permitted (30m); focused checks limited to 5m total.' if record['testing_policy'] == 'full' else 'focused checks (60s each, 5m total); no full-suite runs.'))
        self.manifest['phases'][stage] = record
        self.active = stage
        self.save()
        cwd = self.scratch / 'auth-probe' if probe else self.worktree
        cwd.mkdir(exist_ok=True) if probe else None
        commands = read_json(self.path / 'logs/commands.json', [])
        commands.append(dict(stage=stage, time=now(), cwd=str(cwd), argv=argv))
        write_json(self.path / 'logs/commands.json', commands)
        before = set()
        if not probe:
            self.repo.verify(self.manifest, self.path)
            before = set(self.repo.git('status', '--porcelain=v1', '--untracked-files=all', '--ignored', cwd=self.worktree).stdout.splitlines())
            atomic_bytes(self.path / f'logs/{stage}.files-before.txt', ('\n'.join(sorted(before)) + ('\n' if before else '')).encode())
            identity = process(['git', '-c', 'core.hooksPath=/dev/null', '-C', self.worktree, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir', 'HEAD'], env=env)
            atomic_bytes(self.path / f'logs/{stage}.git-preflight.log', identity.stdout.encode())
            atomic_bytes(self.path / f'logs/{stage}.git-preflight.log.stderr', identity.stderr.encode())
            if identity.returncode or identity.stdout.splitlines() != [str(self.worktree), str(self.repo.common), self.manifest['snapshot']['head']]:
                raise ApiError('integrity_failed', f"Git cannot read the worktree with {provider}'s filtered environment. See {self.path}/logs/{stage}.git-preflight.log")
        try:
            result = process(argv, cwd=cwd, env=env, input_bytes=input_bytes,
                             timeout=self.runtime.auth_timeout if probe else self.runtime.model_timeout,
                             stdout_path=output, stderr_path=stderr, model_events=True, stage=stage)
        except Cancelled as exc:
            record.update(status='interrupted', exit_status=exc.details['exit_code'], finished_at=now())
            self.active = None
            self.save()
            raise
        record.update(exit_status=result.returncode, finished_at=now())
        if result.reason:
            record.update(failure_reason=result.reason, process_exit_status=result.process_exit_status)
        if not probe:
            self.repo.verify(self.manifest, self.path)
            after = set(self.repo.git('status', '--porcelain=v1', '--untracked-files=all', '--ignored', cwd=self.worktree).stdout.splitlines())
            atomic_bytes(self.path / f'logs/{stage}.files-after.txt', ('\n'.join(sorted(after)) + ('\n' if after else '')).encode())
            if after - before:
                self.manifest['generated_files'] = sorted(set(self.manifest.get('generated_files', [])) | (after - before))
                say('Warning: Model created untracked or ignored files (retained): ' + '\n'.join(sorted(after - before)))
        self.save()
        if result.returncode:
            self.model_failure(stage, provider, result.returncode, output, stderr, probe)
        return result, output

    def model_failure(self, stage, provider, rc, output, stderr, probe=False):
        text = output.read_text(errors='replace') + stderr.read_text(errors='replace')
        record = self.manifest['phases'][stage]
        record.update(status='failed')
        say(f'Warning: {stage} failed with exit {rc} ({provider}, model: {record["model"]}).')
        events = []
        for line in output.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get('type') == 'result':
                    events.append(event)
            except ValueError:
                pass
        subtype = events[-1].get('subtype') if events else None
        if isinstance(subtype, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', subtype):
            say('Warning: CLI result subtype: ' + subtype)
        if probe:
            reason = 'timeout' if rc in (124, 137) else 'unverified'
            for name, pattern in [('authentication', r'oauth session expired|unauthorized|authentication|failed to authenticate|invalid.{0,20}(api.?key|token)|token.{0,20}expired|401'),
                                  ('quota', r'quota|rate.limit|usage.limit|insufficient.credit|429'),
                                  ('model_access', r'model.{0,80}(not found|not available|unavailable|not supported|does not exist|access)|unsupported.model'),
                                  ('network', r'connection|network|dns|timed out|unable to connect')]:
                if reason != 'timeout' and re.search(pattern, text, re.I):
                    reason = name
                    break
            record['failure_reason'] = reason
            messages = dict(authentication=f"Live authentication failed. Run '{'claude auth login' if provider == 'claude' else 'codex login'}' and retry.",
                quota='Live probe blocked by a usage or rate limit; authentication could not be confirmed.',
                model_access='Probe model unavailable or not permitted; set PR_REVIEW_CODEX_PROBE_MODEL to an available inexpensive model.',
                network='Live probe could not reach the service; authentication could not be confirmed.',
                timeout='Live probe timed out; adjust PR_REVIEW_AUTH_TIMEOUT if needed.',
                unverified='Live probe did not return a successful diagnostic response; authentication could not be confirmed.')
            say('Warning: ' + messages[reason])
        if provider == 'claude' and re.search('OAuth session expired', text, re.I):
            record['failure_reason'] = 'oauth_session_expired'
            say("Warning: Claude's OAuth session expired. Run 'claude auth login', then resume this run.")
        if '"is_error": true' in text or '"is_error":true' in text:
            say('Warning: CLI result reports an error (is_error=true), regardless of its subtype.')
        if record.get('failure_reason') == 'git_repository_unavailable':
            say(f'Warning: Codex rejected the working directory as a Git repository: {self.worktree}')
        say(f'Captured stdout (including structured errors): {output}')
        say(('Captured stderr: ' if stderr.stat().st_size else 'Captured stderr is empty: ') + str(stderr))
        self.active = None
        self.save()
        raise ApiError('model_failed', 'Live authentication preflight failed; review setup stopped' if probe else f'{stage} failed; inspect captured logs and resume to retry')

    def complete(self, stage, output):
        if not output.is_file() or not output.stat().st_size:
            raise ApiError('model_failed', f'{stage} produced an empty report')
        self.manifest['phases'][stage].update(status='completed', output_hash=hash_file(output), exit_status=0)
        self.active = None
        self.save()

    def authenticate_live(self, enabled):
        for provider in ('claude', 'codex'):
            if provider not in enabled:
                continue
            stage = provider + '_auth_probe'
            response = self.path / f'logs/{stage}.response.txt'
            response.unlink(missing_ok=True)
            say(f'Checking live {provider} access...')
            result, output = self.model(stage, providers.probe_command(provider, self.runtime, response), probe=True)
            events = result.events
            values = [e for e in events if e.get('type') == 'result']
            valid = (bool(values) and values[-1].get('subtype') == 'success' and values[-1].get('is_error') is False and values[-1].get('result') == 'PR_REVIEW_AUTH_OK') if provider == 'claude' else (
                response.is_file() and response.read_text().rstrip('\n') == 'PR_REVIEW_AUTH_OK' and any(e.get('type') == 'turn.completed' for e in events) and not any(e.get('type') in ('error', 'turn.failed') for e in events))
            if not valid:
                self.model_failure(stage, provider, 0, output, self.path / f'logs/{stage}.stderr.log', True)
            self.complete(stage, output)
            say(f'Live {provider} access confirmed.')

    def snapshot(self, pr):
        if self.manifest.get('snapshot_update'):
            self.finish_snapshot_update()
        for attempt in range(2):
            branch = pr['baseRefName']
            source = 'refs/heads/' + branch if pr['state'] == 'OPEN' else pr['baseRefOid']
            self.repo.git('fetch', '--no-tags', '--refmap=', '--no-write-fetch-head', self.repo.remote,
                          f'+refs/pull/{self.repo.pr}/head:{self.repo.head_ref}', f'+{source}:{self.repo.base_ref}')
            pr = self.repo.metadata()
            head = self.repo.git('rev-parse', self.repo.head_ref).stdout.strip()
            base = self.repo.git('rev-parse', self.repo.base_ref).stdout.strip()
            if head == pr['headRefOid'] and base == pr['baseRefOid'] and branch == pr['baseRefName']:
                break
            say(f'Warning: PR refs changed during setup (attempt {attempt+1}); refreshing snapshot')
        else:
            raise ApiError('snapshot_changed', 'Could not establish a consistent snapshot after retry')
        snapshot = dict(head=head, base=base, merge_base=self.repo.git('merge-base', head, base).stdout.strip(), base_branch=branch)
        old = self.manifest.get('snapshot', {})
        if old and any(old[k] != snapshot[k] for k in ('head', 'merge_base')):
            self.update_snapshot(snapshot)
        if not self.worktree.exists() and self.manifest['phases'].get('setup', {}).get('status') != 'completed':
            self.repo.git('worktree', 'add', '--detach', str(self.worktree), head)
        if not self.worktree.is_dir() or self.repo.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=self.worktree).stdout.strip() != str(self.repo.common):
            raise ApiError('invalid_session', 'Resume worktree missing or belongs to a different repository')
        self.manifest['snapshot'] = snapshot
        self.repo.verify(self.manifest, self.path)
        extras = set(self.repo.git('status', '--porcelain=v1', '--untracked-files=all', '--ignored', cwd=self.worktree).stdout.splitlines()) - set(self.manifest.get('generated_files', []))
        if extras:
            raise ApiError('integrity_failed', 'Resume worktree has unrecorded untracked/ignored files; inspect them before resuming: ' + '\n'.join(sorted(extras)))
        self.manifest['phases']['setup'] = dict(status='completed', exit_status=0)
        self.save()
        say(f'Base: {branch} ({base}); Head: {head}; Merge-base: {snapshot["merge_base"]}')
        return pr

    def trust(self, token, confirm=None):
        snapshot = self.manifest['snapshot']
        paths = self.repo.git('diff', '--name-only', '--no-renames', '-z', snapshot['merge_base'], snapshot['head'], cwd=self.worktree).stdout.split('\0')
        changed = [p for p in paths if any(c in ('AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'CLAUDE.local.md', '.claude', '.codex', '.mcp.json') for c in p.split('/'))]
        atomic_bytes(self.path / 'context/agent-config-changes.txt', ('\n'.join(changed) + ('\n' if changed else '')).encode())
        if changed and not self.manifest.get('agent_config_confirmed'):
            expected = snapshot['head'] + ':' + snapshot['merge_base']
            say('Warning: This PR changes agent configuration loaded by the models:\n' + '\n'.join(changed))
            if token != expected and not (confirm and confirm(changed)):
                say(json.dumps(dict(confirmation_required='agent_configuration', snapshot=expected, paths='\n'.join(changed) + '\n')))
                raise ApiError('confirmation_required', 'Agent configuration changes require interactive confirmation', snapshot=expected, paths=changed)
            self.manifest['agent_config_confirmed'] = True
            self.save()

    def context(self, pr, issues, reviews):
        folder = self.path / 'context'
        if self.manifest['phases'].get('context', {}).get('status') == 'completed' and cache.context_hash(folder) == self.manifest['phases']['context'].get('output_hash'):
            return
        for name, value in (('pr', pr), ('pr-discussion', issues), ('pr-reviews', reviews)):
            write_json(folder / (name + '.json'), value)
        for name, endpoint, kind in [('pr-inline-comments', f'pulls/{self.repo.pr}/comments', 'array'),
                                     ('ci-actions-runs', f'actions/runs?head_sha={pr["headRefOid"]}', 'actions'),
                                     ('ci-statuses', f'commits/{pr["headRefOid"]}/statuses', 'array')]:
            try:
                value = self.repo.pages(f'repos/{self.repo.name}/{endpoint}', kind)
            except ApiError as exc:
                if isinstance(exc, Cancelled):
                    raise
                value = None
                warning = f'{name} unavailable; context is incomplete.'
                self.manifest['warnings'] = sorted(set(self.manifest['warnings'] + [warning]))
                say('Warning: ' + warning)
            write_json(folder / (name + '.json'), value)
        rendered = command(['gh', 'pr', 'view', str(self.repo.pr), '-R', self.repo.identity, '--comments'], cwd=self.repo.cwd, check=False)
        if rendered.returncode == 0:
            atomic_bytes(folder / 'pr-comments.txt', rendered.stdout.encode())
        else:
            self.manifest['warnings'].append('Rendered PR comments unavailable; see structured discussion.')
        atomic_bytes(folder / 'pr-view.txt', f"# {pr['title']}\n\n{pr['body']}\n\n{pr['url']}\n".encode())
        snapshot = self.manifest['snapshot']
        for name, args in [('pr.diff', ['--no-ext-diff', '--no-textconv']), ('diffstat.txt', ['--stat']), ('files.zlist', ['--name-only', '-z'])]:
            atomic_bytes(folder / name, self.repo.git('diff', *args, snapshot['merge_base'], snapshot['head'], '--', cwd=self.worktree).stdout.encode())
        self.manifest['phases']['context'] = dict(status='completed', exit_status=0, output_hash=cache.context_hash(folder))
        self.save()
        say(f'Discussion: {len(issues)}; reviews: {len(reviews)}')
        counts = dict(passed=0, failed=0, pending=0)
        for item in read_json(folder / 'ci-actions-runs.json') or []:
            key = 'pending' if item.get('status') != 'completed' else 'passed' if item.get('conclusion') in ('success', 'neutral', 'skipped') else 'failed'
            counts[key] += 1
        latest = {}
        for item in sorted(read_json(folder / 'ci-statuses.json') or [], key=lambda i: i.get('created_at', '')):
            latest[item.get('context')] = item
        for item in latest.values():
            counts[{'success': 'passed', 'pending': 'pending'}.get(item.get('state'), 'failed')] += 1
        say(f"CI: {counts['passed']} passed, {counts['failed']} failed, {counts['pending']} pending." if sum(counts.values()) else 'CI: no Actions runs or statuses found (unavailable sources are reported separately).')
        say('CI coverage excludes check runs from third-party GitHub Apps.')
        say((folder / 'diffstat.txt').read_text())

    def session(self):
        from .state import Session, generation
        return Session({'session': str(self.root), 'generation': generation(self.path, self.manifest)})


def capabilities(enabled, processing, runtime, cwd):
    required = ['git', 'gh', *enabled]
    for tool in required:
        if shutil.which(tool) is None:
            raise ApiError('missing_dependency', f'Missing required executable: {tool}')
    versions = {tool: command([tool, '--version'], cwd=cwd, timeout=90).stdout.strip().splitlines()[0] for tool in required}
    versions.setdefault('codex', 'disabled')
    versions.setdefault('claude', 'disabled')
    checks = [(['gh', 'api', '--help'], ['--hostname', '--paginate', '--slurp', '--method', '--field', '--raw-field']),
              (['gh', 'pr', 'view', '--help'], ['--json', '--comments', '--repo']),
              (['gh', 'auth', 'status', '--help'], ['--hostname']),
              (['git', 'worktree', '-h'], ['--detach']),
              (['git', 'fetch', '-h'], ['--refmap', 'write-fetch-head', '--no-tags'])]
    if 'codex' in enabled:
        checks += [(['codex', 'exec', 'review', '--help'], ['--base', '--output-last-message', '--json', '--ephemeral', '--model', '--config', '--strict-config']),
                   (['codex', 'exec', '--help'], ['--skip-git-repo-check', '--output-last-message', '--json', '--ephemeral'] + (['--output-schema'] if processing == 'codex' else [])),
                   (['codex', 'login', '--help'], ['status'])]
    if 'claude' in enabled:
        checks += [(['claude', '--help'], ['--print', '--model', '--effort', '--permission-mode', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                   '--no-session-persistence', '--disallowed-tools', '--add-dir', '--json-schema', '--append-system-prompt', '--tools', '--disable-slash-commands', '--strict-mcp-config', '--system-prompt', '--setting-sources'] + (['--max-budget-usd'] if runtime.budget else [])),
                   (['claude', 'auth', '--help'], ['status'])]
    for argv, flags in checks:
        result = command(argv, cwd=cwd, check=False, timeout=90)
        for flag in flags:
            if flag not in result.stdout + result.stderr:
                raise ApiError('missing_capability', f'Installed CLI lacks required capability: {flag}')
    say('; '.join(versions.values()))
    return versions


def execute(request, *, confirm_trust=None):
    """Return the selected generation after releasing all backend locks."""
    from . import pipeline
    repo = Repository.resolve(request['pr'], request.get('repository'), request.get('cwd'))
    runtime = providers.Runtime.environment()
    threads = request.get('followup_threads', [])
    if not isinstance(threads, list) or any(not isinstance(k, str) or not k.startswith('thread:') or len(k) <= 7 for k in threads) or len(threads) != len(set(threads)):
        raise ApiError('invalid_request', 'Select distinct existing conversations for follow-up.')
    threads = sorted(threads)
    kind = 'followup' if threads else 'full'
    with cancellation_scope(), locked(repo.lock):
        root, worktree = locate(repo, request.get('session'))
        from .automation import check_existing
        check_existing(root)
        configuration = dict(settings.DEFAULTS)
        configuration.update({k: v for k, v in request.get('defaults', {}).items() if k in settings.DEFAULTS and v is not None})
        if root:
            from .sessions import selected
            configuration.update(settings.from_manifest(read_json(selected(root) / 'manifest.json', {})))
        configuration.update({k: request[k] for k in settings.DEFAULTS if k in request})
        configuration = settings.validate(configuration)
        if request.get('catalogs') is not None:
            from .models import validate_selection
            validate_selection(configuration, request['catalogs'])
        enabled = [configuration['processing_provider']] if kind == 'followup' else configuration['reviewers']
        tools = capabilities(enabled, configuration['processing_provider'], runtime, repo.cwd)
        say(f'PR Review: #{repo.pr}; Repository: {repo.identity}; Remote: {repo.remote}')
        checks = [(['gh', 'auth', 'status', '--hostname', repo.host], f'GitHub authentication failed for {repo.host}')]
        if 'codex' in enabled:
            checks.append((['codex', 'login', 'status'], 'Codex authentication failed; run codex login'))
        if 'claude' in enabled:
            checks.append((['claude', 'auth', 'status'], 'Claude authentication failed; run claude auth login'))
        for argv, error in checks:
            if command(argv, cwd=repo.cwd, check=False, timeout=runtime.auth_timeout).returncode:
                raise ApiError('authentication_failed', error)
        pr = repo.metadata()
        viewer = command(['gh', 'api', '--hostname', repo.host, 'user', '--jq', '.login'], cwd=repo.cwd).stdout.strip()
        if not viewer:
            raise ApiError('authentication_failed', 'GitHub viewer login missing')
        permission = command(['gh', 'repo', 'view', repo.identity, '--json', 'viewerPermission', '--jq', '.viewerPermission'], cwd=repo.cwd, check=False).stdout.strip() or 'unknown'
        say(f'Warning: Read access is checked; review-publication permission is unverified (repository permission: {permission}).')
        issues = repo.pages(f'repos/{repo.name}/issues/{repo.pr}/comments')
        reviews = repo.pages(f'repos/{repo.name}/pulls/{repo.pr}/reviews')
        repo.git('ls-remote', '--exit-code', repo.remote, f'refs/pull/{repo.pr}/head')
        repo.git('ls-remote', '--exit-code', repo.remote, 'refs/heads/' + pr['baseRefName'])
        if pr['isDraft']:
            say('Warning: PR is a draft')
        run = Review(repo, root, worktree, configuration, runtime, tools, viewer, permission)
        try:
            run.configure(kind, threads)
            with tempfile.TemporaryDirectory(prefix='pr-review-') as scratch:
                run.scratch = Path(scratch)
                run.authenticate_live(enabled)
                pr = run.snapshot(pr)
                run.trust(request.get('trusted_snapshot'), confirm_trust)
                run.context(pr, issues, reviews)
                if kind == 'followup':
                    pipeline.focused(run)
                    ready = True
                else:
                    if run.dual:
                        pipeline.restart(run)
                        pipeline.independent(run, 'claude')
                        pipeline.independent(run, 'codex')
                    pipeline.synthesis(run)
                    ready = pipeline.contributions(run)
                run.manifest['status'] = 'complete' if ready else 'paused'
                if ready:
                    from .automation import manual_completion
                    manual_completion(run)
                run.save()
        except Cancelled as exc:
            if run.active:
                run.manifest['phases'][run.active].update(status='interrupted', exit_status=exc.details['exit_code'], finished_at=now())
                run.active = None
            run.manifest['status'] = 'failed'
            run.save()
            raise
        except BaseException:
            if run.active:
                record = run.manifest['phases'][run.active]
                record.update(status='failed', exit_status=record.get('exit_status', 1), finished_at=now())
            run.manifest['status'] = 'failed'
            run.save()
            raise
        finally:
            report = run.path / ('reviews/followup.json' if kind == 'followup' else 'reviews/synthesis.md')
            say(f'\nPR:        {repo.url}\nWorktree:  {worktree}\nArtifacts: {run.path}\nReview:    {report}\nStatus:    {run.manifest["status"]}')
            if run.pending:
                say(f'Previous review: {run.current} (replacement not installed)\nResume session: {run.root}')
    return run.session()
