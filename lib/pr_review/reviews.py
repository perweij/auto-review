"""Shared review service. Terminal and API invoke this directly, without a CLI subprocess."""
import json
from contextlib import ExitStack
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import usage
from .common import ApiError, command, locked, now, read_json
from . import cache, providers, review_engine, settings, validation
from .processes import Cancelled, cancellation_scope, run as process
from .review_engine import requires_independent
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
        return len(self.settings['reviewers']) == 2

    def command(self, stage):
        return providers.review_command(self.settings, self.runtime, stage, self.path, self.repo.base_ref)

    def configure(self, kind, threads):
        self.scope(kind, threads)
        if (settings.effective_review(settings.from_manifest(self.manifest)) != settings.effective_review(self.settings) and
                any(self.manifest['phases'].get(s, {}).get('status') == 'completed' for s in ('synthesis', 'followup'))):
            self.begin()
        if getattr(self, 'resuming', False) and 'legacy_review_artifacts' not in self.manifest:
            mapping = settings.legacy_review_artifacts(self.manifest)
            if mapping is not None:
                self.manifest['legacy_review_artifacts'] = mapping
        self.manifest.update(configuration=self.settings, tools=self.tools,
            models=dict(codex=self.settings['codex_model'], claude=self.settings['claude_model'],
                        claude_effort=self.settings['claude_effort'], codex_effort=self.settings['codex_effort'], claude_permission_mode=self.runtime.permission),
            viewer=self.viewer, viewer_permission=self.permission, status='running')
        self.save()
        say(f'Artifacts: {self.path}')

    @property
    def adapter(self):
        return PR_ADAPTER

    def model(self, stage, argv, input_bytes=b'', probe=False):
        if not probe:
            return review_engine.execute_stage(self, stage, argv, input_bytes, runner=process)
        provider = stage.split('_')[0]
        model = 'haiku' if provider == 'claude' else self.runtime.probe_model
        output = self.path / f'logs/{stage}.jsonl'
        stderr = self.path / f'logs/{stage}.stderr.log'
        env, removed = providers.environment(provider)
        self.manifest.setdefault('removed_environment', {})[provider] = removed
        record = dict(status='running', fingerprint='', started_at=now(), stdout_log=str(output), stderr_log=str(stderr), model=model)
        self.manifest['phases'][stage] = record
        self.active = stage
        self.save()
        cwd = self.scratch / 'auth-probe'
        cwd.mkdir(exist_ok=True)
        commands = read_json(self.path / 'logs/commands.json', [])
        commands.append(dict(stage=stage, time=now(), cwd=str(cwd), argv=argv))
        write_json(self.path / 'logs/commands.json', commands)
        try:
            result = process(argv, cwd=cwd, env=env, input_bytes=input_bytes, timeout=self.runtime.auth_timeout,
                             stdout_path=output, stderr_path=stderr, model_events=True, stage=stage, pass_fds=(), usage_call=usage.choice('auth-probe/' + provider,
                                 {'provider': provider, 'model': model, 'effort': 'low'}))
        except ApiError as exc:
            record.update(status='interrupted' if isinstance(exc, Cancelled) or exc.code == 'resource_limit' else 'failed',
                          exit_status=exc.details.get('exit_code', record.get('exit_status', 1)), finished_at=now())
            self.active = None
            raise
        finally:
            self.save()
        record.update(exit_status=result.returncode, finished_at=now())
        if result.reason:
            record.update(failure_reason=result.reason, process_exit_status=result.process_exit_status)
        self.save()
        if result.returncode:
            self.model_failure(stage, provider, result.returncode, output, stderr, True)
        responses = [e.get('item', {}).get('text', '') for e in result.events
                     if e.get('type') == 'item.completed' and isinstance(e.get('item'), dict)
                     and e['item'].get('type') == 'agent_message']
        responses += [json.dumps(e.get('structured_output') or e.get('result', ''))
                      for e in result.events if e.get('type') == 'result']
        if responses and providers.reported_execution_failure(responses[-1]):
            record.update(status='failed', failure_reason='sandbox_unavailable')
            self.active = None
            self.save()
            raise ApiError('model_failed', f"{stage}: model reported a sandbox launch failure "
                           "(bwrap cannot mount /proc). Review inputs could not be inspected. "
                           f"The provider environment is incompatible with outer-container execution; check managed sandbox policy and provider diagnostics before retrying. See {output}")
        return result, output

    def record_generated_files(self, stage, before):
        result = subprocess.run(
            ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
             'status', '--porcelain=v1', '--untracked-files=all', '--ignored'],
            cwd=self.worktree, capture_output=True, text=True, timeout=10, check=True)
        after = set(result.stdout.splitlines())
        atomic_bytes(self.path / f'logs/{stage}.files-after.txt',
                     ('\n'.join(sorted(after)) + ('\n' if after else '')).encode())
        generated = {line for line in after - before if line.startswith(('?? ', '!! '))}
        if generated:
            self.manifest['generated_files'] = sorted(set(self.manifest.get('generated_files', [])) | generated)
            say('Warning: Model created untracked or ignored files (retained): ' + '\n'.join(sorted(generated)))

    def model_failure(self, stage, provider, rc, output, stderr, probe=False):
        review_engine.model_failure(self, stage, provider, rc, output, stderr, probe)

    def complete(self, stage, output):
        review_engine.complete(self, stage, output)

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
                          f'+{self.repo.forge.head_ref(self.repo.pr)}:{self.repo.head_ref}', f'+{source}:{self.repo.base_ref}')
            pr = self.repo.metadata()
            head = self.repo.git('rev-parse', self.repo.head_ref).stdout.strip()
            base = self.repo.git('rev-parse', self.repo.base_ref).stdout.strip()
            # PR metadata can retain an older baseRefOid after the base branch
            # advances.  Open reviews use the live branch, so validate it via
            # Git too; closed reviews deliberately use the recorded commit.
            expected_base = pr['baseRefOid']
            if pr['state'] == 'OPEN':
                ref = 'refs/heads/' + pr['baseRefName']
                refs = self.repo.git('ls-remote', '--refs', self.repo.remote, ref).stdout.splitlines()
                expected_base = next((line.split()[0] for line in refs
                                      if len(line.split()) == 2 and line.split()[1] == ref), None)
            if head == pr['headRefOid'] and base == expected_base and branch == pr['baseRefName']:
                break
            say(f'Warning: PR refs changed during setup (attempt {attempt+1}); refreshing snapshot; '
                f'fetched head={head}, base={base}, base_branch={branch}; '
                f'metadata head={pr["headRefOid"]}, base={pr["baseRefOid"]}, '
                f'base_branch={pr["baseRefName"]}, state={pr["state"]}; expected_base={expected_base}')
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


def capabilities(enabled, processing, runtime, cwd, *, independent_providers=None, forge=None):
    independent_providers = enabled if independent_providers is None else independent_providers
    from .forge import adapter
    selected_forge = adapter(forge)
    required = ['git', selected_forge.executable, *enabled]
    for tool in required:
        if shutil.which(tool) is None:
            raise ApiError('missing_dependency', f'Missing required executable: {tool}')
    versions = {tool: command([tool, '--version'], cwd=cwd, timeout=90).stdout.strip().splitlines()[0] for tool in required}
    versions.setdefault('codex', 'disabled')
    versions.setdefault('claude', 'disabled')
    checks = selected_forge.cli_checks() + [
              (['git', 'worktree', '-h'], ['--detach']),
              (['git', 'fetch', '-h'], ['--refmap', 'write-fetch-head', '--no-tags'])]
    if 'codex' in enabled:
        if 'codex' in independent_providers:
            checks.append((['codex', 'exec', 'review', '--help'], ['--base', '--output-last-message', '--json', '--ephemeral', '--model', '--config', '--strict-config']))
        checks += [(['codex', 'exec', '--help'], ['--skip-git-repo-check', '--output-last-message', '--json', '--ephemeral'] + (['--output-schema'] if processing == 'codex' else [])),
                   (['codex', 'login', '--help'], ['status'])]
    if 'claude' in enabled:
        checks += [(['claude', '--help'], ['--print', '--model', '--effort', '--permission-mode', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                   '--no-session-persistence', '--disallowed-tools', '--add-dir', '--json-schema', '--append-system-prompt', '--tools', '--disable-slash-commands', '--strict-mcp-config', '--system-prompt', '--setting-sources', '--settings'] + (['--max-budget-usd'] if runtime.budget else [])),
                   (['claude', 'auth', '--help'], ['status'])]
    for argv, flags in checks:
        result = command(argv, cwd=cwd, check=False, timeout=90)
        for flag in flags:
            if flag not in result.stdout + result.stderr:
                raise ApiError('missing_capability', f'Installed CLI lacks required capability: {flag}')
    say('; '.join(versions.values()))
    return versions


def required_providers(configuration, kind):
    """Providers used by the selected review stages, without duplicates."""
    providers_needed = [configuration['processing_provider']]
    if kind != 'followup':
        providers_needed = [slot['provider'] for slot in configuration['reviewer_slots']] + providers_needed
    return list(dict.fromkeys(providers_needed))


def execute(request, *, confirm_trust=None):
    """Return the selected generation after releasing all backend locks."""
    from . import pipeline
    from .forge import require, request_forge
    request = {**request, 'forge': request_forge(request)}
    require(request.get('forge'), 'review')
    repo = Repository.resolve(request['pr'], request.get('repository'), request.get('cwd'), request.get('forge'))
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
        configuration = settings.initial_settings(request)
        saved_manifest = {}
        if root:
            from .sessions import selected
            saved_manifest = read_json(selected(root) / 'manifest.json', {})
            configuration.update(settings.validate(settings.from_manifest(saved_manifest)))
        settings.apply_overrides(configuration, request, fresh=not root)
        configuration = settings.validate(configuration)
        if request.get('catalogs') is not None:
            from .models import validate_selection
            validate_selection(configuration, request['catalogs'], reviewers=kind != 'followup')
        enabled = required_providers(configuration, kind)
        independent_providers = (configuration['reviewers'] if kind != 'followup' and
                                 requires_independent(configuration, saved_manifest) else [])
        tools = capabilities(enabled, configuration['processing_provider'], runtime, repo.cwd,
                             independent_providers=independent_providers, forge=repo.project.forge)
        if request.get('_cli_config'):
            from .cli_settings import summary, validate_models
            validate_models(configuration, request.get('catalogs'), reviewers=kind != 'followup')
            summary(configuration, runtime, request=request)
        say(f'PR Review: #{repo.pr}; Repository: {repo.identity}; Remote: {repo.remote}')
        checks = [(repo.forge.auth_check(repo.project), f'GitHub authentication failed for {repo.host}')]
        if 'codex' in enabled:
            checks.append((['codex', 'login', 'status'], 'Codex authentication failed; run codex login'))
        if 'claude' in enabled:
            checks.append((['claude', 'auth', 'status'], 'Claude authentication failed; run claude auth login'))
        for argv, error in checks:
            if command(argv, cwd=repo.cwd, check=False, timeout=runtime.auth_timeout).returncode:
                raise ApiError('authentication_failed', error)
        pr = repo.metadata()
        account = repo.forge.account(repo.project, repo.cwd)
        viewer = account['username']
        if not viewer:
            raise ApiError('authentication_failed', 'GitHub viewer login missing')
        permission = command(['gh', 'repo', 'view', repo.identity, '--json', 'viewerPermission', '--jq', '.viewerPermission'], cwd=repo.cwd, check=False).stdout.strip() or 'unknown'
        say(f'Warning: Read access is checked; review-publication permission is unverified (repository permission: {permission}).')
        issues = repo.pages(f'repos/{repo.name}/issues/{repo.pr}/comments')
        reviews = repo.pages(f'repos/{repo.name}/pulls/{repo.pr}/reviews')
        repo.git('ls-remote', '--exit-code', repo.remote, repo.forge.head_ref(repo.pr))
        repo.git('ls-remote', '--exit-code', repo.remote, 'refs/heads/' + pr['baseRefName'])
        if pr['isDraft']:
            say('Warning: PR is a draft')
        run = Review(repo, root, worktree, configuration, runtime, tools, viewer, permission)
        from .forge import check_account
        check_account(run.manifest.get("forge_account"), account, run.manifest.get("viewer"))
        if not root:
            run.manifest["forge_account"] = account
            run.save()
        with usage.operation(run.root, legacy=root is not None):
            try:
                run.configure(kind, threads)
                from .checkouts import announce
                from .state import generation
                announce(run.worktree, run.root, "review", generation(run.path, run.manifest))
                with tempfile.TemporaryDirectory(prefix='pr-review-') as scratch:
                    run.scratch = Path(scratch)
                    run.authenticate_live(enabled)
                    pr = run.snapshot(pr)
                    announce(run.worktree, run.root, "review", generation(run.path, run.manifest))
                    run.trust(request.get('trusted_snapshot'), confirm_trust)
                    run.context(pr, issues, reviews)
                    if kind == 'followup':
                        pipeline.focused(run)
                        ready = True
                    else:
                        if run.manifest.get('review_restart'):
                            pipeline.restart(run)
                        slots = configuration['reviewer_slots']
                        independent = requires_independent(configuration, run.manifest)
                        if independent:
                            pipeline.adopt_legacy_slots(run, slots)
                            pipeline.prepare_slots(run, slots)
                            for slot in slots:
                                pipeline.independent(run, slot['id'])
                        pipeline.synthesis(run)
                        ready = pipeline.contributions(run)
                        if ready and configuration['suggested_changes']:
                            from .suggestions import generate
                            generate(run)
                    run.manifest['status'] = 'complete' if ready else 'paused'
                    if ready:
                        from .automation import manual_completion
                        manual_completion(run)
                    run.save()
            except BaseException as exc:
                review_engine.record_interruption(run, exc)
                run.manifest['status'] = 'failed'
                run.save()
                raise
            finally:
                report = run.path / ('reviews/followup.json' if kind == 'followup' else 'reviews/synthesis.md')
                say(f'\nPR:        {repo.url}\nArtifacts: {run.path}\nReview:    {report}\nStatus:    {run.manifest["status"]}')
                if run.pending:
                    say(f'Previous review: {run.current} (replacement not installed)\nResume session: {run.root}')
    return run.session()


class PrAdapter(review_engine.Adapter):
    """PR review: existing prompts, schema, argv, fingerprints and snapshot verification."""
    name = 'pr'

    def log_paths(self, run, stage):
        output = run.path / ('reviews/claude-independent.jsonl' if stage == 'claude' else
                             'logs/codex.events.jsonl' if stage == 'codex' else f'logs/{stage}.raw.json')
        stderr = run.path / ('logs/claude-independent.stderr.log' if stage == 'claude' else f'logs/{stage}.stderr.log')
        return output, stderr

    def stage_cwd(self, run, stage, slot):
        return run.worktree

    def verify_snapshot(self, run, stage, when, context=None):
        # Saved PR sessions are verified when their snapshot is established.
        if when in ('before', 'after'):
            run.repo.verify(run.manifest, run.path)

    def begin_stage(self, run, stage, slot, provider, env):
        self.verify_snapshot(run, stage, 'before')
        before = set(run.repo.git('status', '--porcelain=v1', '--untracked-files=all', '--ignored', cwd=run.worktree).stdout.splitlines())
        atomic_bytes(run.path / f'logs/{stage}.files-before.txt', ('\n'.join(sorted(before)) + ('\n' if before else '')).encode())
        identity = process(['git', '-c', 'core.hooksPath=/dev/null', '-C', run.worktree, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir', 'HEAD'], env=env)
        atomic_bytes(run.path / f'logs/{stage}.git-preflight.log', identity.stdout.encode())
        atomic_bytes(run.path / f'logs/{stage}.git-preflight.log.stderr', identity.stderr.encode())
        if identity.returncode or identity.stdout.splitlines() != [str(run.worktree), str(run.repo.common), run.manifest['snapshot']['head']]:
            raise ApiError('integrity_failed', f"Git cannot read the worktree with {provider}'s filtered environment. See {run.path}/logs/{stage}.git-preflight.log")
        return before

    def end_stage(self, run, stage, slot, context):
        run.record_generated_files(stage, context)

    def render_synthesis(self, run, correction=None):
        from . import pipeline
        pipeline.render_synthesis(run)

    def structured(self, run, stage):
        from . import pipeline
        return pipeline.structured(run, stage)

    def validate(self, run, value, *, new):
        return validation.synthesis(value, new=new)

    def prepare_rerun(self, run):
        from . import pipeline
        pipeline.protect(run, 'synthesis')
        run.reset_publication()

    def accept_new(self, run, value):
        # A newly produced analysis gets a fresh discussion match, even when a
        # provider happened to return byte-identical findings to its predecessor.
        run.manifest['phases'].pop('contributions', None)

    def finalize(self, run, value):
        from . import pipeline
        pipeline.write_report(run, value)

    def reuse_message(self, run):
        return 'Reusing completed synthesis' if run.dual else 'Reusing completed structured review'

    def running_message(self, run):
        return f'[synthesis] Running {"adversarial synthesis" if run.dual else "structured review (one reviewer)"} ({run.settings["processing_provider"]})'


PR_ADAPTER = PrAdapter()
