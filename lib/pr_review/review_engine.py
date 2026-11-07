"""The reviewer-and-synthesis engine shared by PR review, issue acceptance and Owner verification.

The engine owns slot preparation and invalidation, isolated independent runs,
synthesis, stage execution, fingerprinted reuse and interruption recording. A
workflow supplies an ``Adapter``: its prompts, output schema and validator, extra
fingerprint identity, snapshot integrity callback and the classification of a
valid result as reusable evidence. Workflow acceptance, GitHub publication and
discussion matching stay outside the engine.

A ``run`` is any object with the attributes the engine reads: ``path``,
``manifest['phases']``, ``settings``, ``save()``, ``command(stage)`` and
``model(stage, argv, input_bytes)``. ``Review`` is the PR run; ``CandidateReview``
(see ``review_workspace``) is the artifact store of Owner and issue stages.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import tempfile

from . import usage
from .common import ApiError, now, read_json
from . import cache, providers
from .processes import Cancelled, run as default_runner
from .sessions import say
from .storage import atomic_bytes, hash_file, write_json

PR_DOWNSTREAM = ('synthesis', 'contributions', 'revision')
REUSABLE = ('completed',)


class Adapter:
    """Workflow behaviour the engine does not own. Defaults suit a plain, unsandboxed workflow."""
    name = 'pr'
    downstream = PR_DOWNSTREAM
    corrections = 0        # synthesis correction attempts after an invalid result (PR 0, others 1)

    # ---- identity ---------------------------------------------------------------
    def identity(self, run, stage):
        """Extra fingerprint identity. PR fingerprints gain nothing."""
        return {}

    def artifacts(self, run, stage):
        """Prompt, validator and schema identity for ``stage``; ``None`` keeps the PR scheme."""
        return None

    # ---- stage execution ----------------------------------------------------------
    def log_paths(self, run, stage):
        return run.path / f'logs/{stage}.raw.json', run.path / f'logs/{stage}.stderr.log'

    def stage_cwd(self, run, stage, slot):
        raise NotImplementedError

    def begin_stage(self, run, stage, slot, provider, env):
        """Verify the snapshot and prepare the stage; the result is passed to the later hooks."""
        return None

    def end_stage(self, run, stage, slot, context):
        """Retain generated files after the child stops, even on cancellation."""

    def workspace_inputs(self, run, stage, slot):
        """Inputs for ``IndependentWorkspace``; ``None`` describes a PR session."""
        return None

    def verify_snapshot(self, run, stage, when, context=None):
        """Raise when the assessed snapshot changed. ``when`` is before, after or reuse."""

    def interrupted(self, run, stage, context):
        """Called after a stage was cancelled or hit a limit, once its child has stopped."""

    # ---- synthesis ------------------------------------------------------------------
    def render_synthesis(self, run, correction=None):
        raise NotImplementedError

    def structured(self, run, stage):
        raise NotImplementedError

    def validate(self, run, value, *, new):
        raise NotImplementedError

    def correctable(self, error):
        return False

    def classify(self, value):
        """``completed`` results are reusable evidence; anything else is retained only."""
        return 'completed'

    def prepare_rerun(self, run):
        """Called before a synthesis that cannot be reused is produced."""

    def accept_new(self, run, value):
        """Called with a newly produced, reusable synthesis before it is saved."""

    def finalize(self, run, value):
        """Called with the reusable synthesis, whether produced now or reused."""

    def reuse_message(self, run):
        return 'Reusing completed synthesis'

    def running_message(self, run):
        return 'Running synthesis'


@dataclass
class Result:
    """A synthesis outcome. ``completed`` is reusable evidence; the rest is retained for inspection."""
    value: dict
    status: str
    artifact: Path
    reused: bool = False


def adapter_of(run):
    adapter = getattr(run, 'adapter', None)
    if adapter is None:
        from .reviews import PrAdapter
        adapter = PrAdapter()
    return adapter


def slot_of(run, stage):
    return next((item for item in run.settings.get('reviewer_slots', []) if item['id'] == stage), None)


def requires_independent(configuration, manifest):
    slots = configuration['reviewer_slots']
    reviewer = {key: slots[0][key] for key in ('provider', 'model', 'effort')}
    return (len(slots) == 2 or reviewer != configuration['processing'] or
            any(slot['id'] in manifest.get('phases', {}) for slot in slots))


def complete(run, stage, output):
    if not output.is_file() or not output.stat().st_size:
        raise ApiError('model_failed', f'{stage} produced an empty report')
    run.manifest['phases'][stage].update(status='completed', output_hash=hash_file(output), exit_status=0)
    run.active = None
    run.save()


# ---- interruption --------------------------------------------------------------------
def record_interruption(run, exc):
    """Mark the active stage after an exception. Cancellation is an interruption, other errors failures."""
    active = getattr(run, 'active', None)
    if not active:
        return
    record = run.manifest['phases'].get(active)
    if record is not None:
        if isinstance(exc, Cancelled):
            record.update(status='interrupted', exit_status=exc.details['exit_code'], finished_at=now())
        else:
            record.update(status='failed', exit_status=record.get('exit_status', 1), finished_at=now())
    run.active = None


# ---- slot preparation and invalidation -------------------------------------------------
def prepare_slots(run, slots, downstream=PR_DOWNSTREAM, *, force=()):
    """Preserve invalid slot and downstream artifacts without invalidating peers."""
    journal = run.manifest.get('slot_restart')
    if not journal:
        invalid = []
        for slot in slots:
            stage = slot['id']
            output = run.path / 'reviews' / (stage + '.md')
            if not cache.reusable(run, stage, output, cache.inputs(run, stage)):
                invalid.append(stage)
        invalid.extend(stage for stage in force if stage not in invalid)
        phases = run.manifest['phases']
        if not invalid or not (any((run.path / 'reviews' / (stage + '.md')).exists() or stage in phases for stage in invalid)
                               or any(stage in phases for stage in downstream)):
            return
        run.reset_publication()
        run.begin()
        history = run.path / 'restart-history'
        if history.is_symlink():
            raise ApiError('invalid_artifact', 'Invalid slot restart preservation directory')
        history.mkdir(exist_ok=True, mode=0o700)
        if history.resolve().parent != run.path.resolve():
            raise ApiError('invalid_artifact', 'Invalid slot restart preservation directory')
        archive = Path(tempfile.mkdtemp(prefix='slots-', dir=history))
        affected = list(dict.fromkeys(invalid + list(downstream)))
        files = []
        for folder in ('reviews', 'prompts', 'logs'):
            for path in (run.path / folder).iterdir():
                if path.is_file() and any(path.name.startswith(name + '.') or path.name.startswith(name + '-') for name in affected):
                    files.append(str(path.relative_to(run.path)))
        journal = dict(stages=affected, files=files, archive=str(archive))
        run.manifest['slot_restart'] = journal
        run.save()
    reviewers = [stage for stage in journal['stages'] if stage.startswith('reviewer-')]
    say(('Restarting reviewer slots: ' + ', '.join(reviewers) if reviewers else
         'Restarting review stages: ' + ', '.join(journal['stages']))
        + f". Previous artifacts are preserved in: {journal['archive']}")
    archive = Path(journal['archive']).resolve()
    history = run.path / 'restart-history'
    if history.is_symlink() or history.resolve().parent != run.path.resolve() or archive.parent != history.resolve():
        raise ApiError('invalid_artifact', 'Invalid slot restart preservation path')
    if any(stage not in ('reviewer-1', 'reviewer-2', *downstream) for stage in journal['stages']):
        raise ApiError('invalid_artifact', 'Invalid slot restart stage')
    # Keep the evidence's original execution policy alongside archived reports.
    metadata = archive / 'phases.json'
    if metadata.is_symlink():
        raise ApiError('invalid_artifact', 'Invalid archived phase metadata')
    if not metadata.exists():
        write_json(metadata, {stage: run.manifest['phases'][stage] for stage in journal['stages']
                              if stage in run.manifest['phases']})
    for name in journal['files']:
        relative = Path(name)
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] not in ('reviews', 'prompts', 'logs'):
            raise ApiError('invalid_artifact', 'Invalid slot restart artifact')
        if not any(relative.name.startswith(stage + '.') or relative.name.startswith(stage + '-') for stage in journal['stages']):
            raise ApiError('invalid_artifact', 'Invalid slot restart artifact')
        source, destination = run.path / relative, archive / relative
        if (source.parent.is_symlink() or source.parent.resolve() != run.path.resolve() / relative.parts[0]
                or source.is_symlink() or destination.is_symlink()):
            raise ApiError('invalid_artifact', 'Symlink in slot restart artifacts')
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.resolve() != archive / relative.parts[0]:
                raise ApiError('invalid_artifact', 'Invalid slot restart destination')
            if not destination.exists():
                atomic_bytes(destination, source.read_bytes())
            elif not destination.is_file() or hash_file(source) != hash_file(destination):
                raise ApiError('invalid_artifact', 'Slot restart artifact changed during preservation')
            source.unlink()
    for stage in journal['stages']:
        run.manifest['phases'].pop(stage, None)
    run.manifest.pop('reassessment', None)
    run.manifest.setdefault('review_history', []).append(dict(stages=journal['stages'], archive=str(archive), restarted_at=now()))
    run.manifest.pop('slot_restart')
    run.save()


# ---- structured output ------------------------------------------------------------------
def claude_result(run, stage, result):
    values = [e for e in result.events if e.get('type') == 'result']
    if not values or values[-1].get('is_error') is True or values[-1].get('subtype') != 'success':
        raise ApiError('model_failed', f'{stage} returned no successful Claude result event')
    value = values[-1]
    run.manifest['phases'][stage]['permission_denials'] = value.get('permission_denials', [])
    write_json(run.path / f'logs/{stage}-result.json', value)
    run.save()
    return value


# ---- isolated independent runs --------------------------------------------------------------
def independent(run, stage):
    adapter = adapter_of(run)
    slot = slot_of(run, stage)
    provider = providers.provider_for(run.settings, stage)
    output = run.path / (f'reviews/{stage}.md' if slot else
                         'reviews/claude-independent.md' if stage == 'claude' else 'reviews/codex.md')
    if cache.reusable(run, stage, output, cache.inputs(run, stage)):
        adapter.verify_snapshot(run, stage, 'reuse')
        say(f'Reusing completed {stage.title()} review')
        return
    if stage == 'claude' and (run.path / 'reviews/codex.md').exists():
        raise ApiError('independence_failed', 'Independent Claude inputs/output changed after Codex ran; start a new run to preserve independence')
    output.with_suffix(output.suffix + '.tmp').unlink(missing_ok=True)
    say(f'[{stage}] Running independent {stage.title()} review')
    result, _ = run.model(stage, run.command(stage))
    if provider == 'claude':
        body = claude_result(run, stage, result).get('result')
        if not isinstance(body, str) or not body.strip():
            raise ApiError('model_failed', 'Claude returned an empty independent review')
        atomic_bytes(output, (body + '\n').encode())
    else:
        temp = output.with_suffix('.md.tmp')
        if not temp.is_file() or not temp.stat().st_size:
            raise ApiError('model_failed', 'Codex returned an empty review')
        atomic_bytes(output, temp.read_bytes())
        temp.unlink()
    run.complete(stage, output)


# ---- synthesis ---------------------------------------------------------------------------------
def retain(run, value, assessment):
    """Keep a result that is valid but not reusable under its own per-attempt name.

    It never goes through ``complete`` and never replaces ``reviews/synthesis.json``.
    An earlier retained attempt is not overwritten.
    """
    folder = run.path / 'reviews'
    slug = assessment.replace('_', '-')
    number = 1
    while (folder / f'synthesis-{slug}-{number}.json').exists():
        number += 1
    artifact = folder / f'synthesis-{slug}-{number}.json'
    write_json(artifact, value)
    run.manifest['phases']['synthesis'].update(status=assessment, artifact=str(artifact))
    run.active = None
    run.save()
    return artifact


def reusable_synthesis(run, adapter, output):
    """Return the saved synthesis when it is valid, current and reusable evidence."""
    if not cache.reusable(run, 'synthesis', output, cache.inputs(run, 'synthesis')):
        return None
    try:
        value = read_json(output)
        adapter.validate(run, value, new=False)
    except ApiError:
        return None
    # Defence against older or damaged artifacts: only a reusable assessment counts.
    if adapter.classify(value) not in REUSABLE:
        return None
    return value


def produce_synthesis(run, adapter):
    """Run synthesis, correcting an invalid result on synthesis only."""
    for attempt in range(adapter.corrections + 1):
        try:
            value = adapter.structured(run, 'synthesis')
            adapter.validate(run, value, new=True)
            return value
        except ApiError as exc:
            if attempt >= adapter.corrections or not adapter.correctable(exc):
                raise
            say('Correcting the synthesis result format; reviewer reports are not rerun.')
            adapter.render_synthesis(run, correction=str(exc))


def synthesis(run):
    adapter = adapter_of(run)
    adapter.render_synthesis(run)
    output = run.path / 'reviews/synthesis.json'
    value = reusable_synthesis(run, adapter, output)
    if value is not None:
        adapter.verify_snapshot(run, 'synthesis', 'reuse')
        say(adapter.reuse_message(run))
        status, reused = 'completed', True
    else:
        adapter.prepare_rerun(run)
        adapter.render_synthesis(run)
        say(adapter.running_message(run))
        value = produce_synthesis(run, adapter)
        status, reused = adapter.classify(value), False
        if status not in REUSABLE:
            output = retain(run, value, status)
            return Result(value, status, output)
        adapter.accept_new(run, value)
        output = run.path / 'reviews/synthesis.json'
        write_json(output, value)
        run.complete('synthesis', output)
    value = read_json(output)
    adapter.finalize(run, value)
    return Result(value, status, output, reused)


def review(run):
    """Independent slots when needed, then synthesis, for a non-PR workflow."""
    adapter = adapter_of(run)
    try:
        if requires_independent(run.settings, run.manifest):
            slots = run.settings['reviewer_slots']
            prepare_slots(run, slots, adapter.downstream)
            for slot in slots:
                independent(run, slot['id'])
        return synthesis(run)
    except BaseException as exc:
        record_interruption(run, exc)
        run.save()
        raise


# ---- stage execution -------------------------------------------------------------------------------
def model_failure(run, stage, provider, rc, output, stderr, probe=False):
    text = output.read_text(errors='replace') + stderr.read_text(errors='replace')
    record = run.manifest['phases'][stage]
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
        say(f'Warning: Codex rejected the working directory as a Git repository: {getattr(run, "worktree", "")}')
    say(f'Captured stdout (including structured errors): {output}')
    say(('Captured stderr: ' if stderr.stat().st_size else 'Captured stderr is empty: ') + str(stderr))
    run.active = None
    run.save()
    raise ApiError('model_failed', 'Live authentication preflight failed; review setup stopped' if probe else f'{stage} failed; inspect captured logs and resume to retry')


def execute_stage(run, stage, argv, input_bytes=b'', *, runner=default_runner):
    """Run one provider stage under its adapter's snapshot and isolation rules."""
    adapter = adapter_of(run)
    provider = providers.provider_for(run.settings, stage)
    slot = slot_of(run, stage)
    choice = slot or (run.settings.get('processing') if stage not in ('claude', 'codex') else None)
    model = choice['model'] if choice else run.settings[provider + '_model']
    output, stderr = adapter.log_paths(run, stage)
    env, removed = providers.environment(provider)
    run.manifest.setdefault('removed_environment', {})[provider] = removed
    record = dict(status='running', fingerprint='', started_at=now(), stdout_log=str(output), stderr_log=str(stderr), model=model)
    record['inputs'] = cache.inputs(run, stage)
    if slot:
        # A new invocation is isolated even when replacing an adopted
        # legacy record; its identity must not inherit that provenance.
        record['inputs'].pop('legacy_source', None)
        record['inputs']['isolation'] = 'private-snapshot-container-v1'
    record['fingerprint'] = cache.fingerprint(record['inputs'])
    if stage != 'contributions':
        record['testing_policy'] = run.settings['synthesis_tests'] if stage == 'synthesis' else 'focused'
        say('Verification: ' + ('one full-suite attempt permitted (30m); focused checks limited to 5m total.' if record['testing_policy'] == 'full' else 'focused checks (60s each, 5m total); no full-suite runs.'))
    run.manifest['phases'][stage] = record
    run.active = stage
    run.save()
    cwd = adapter.stage_cwd(run, stage, slot)
    commands = read_json(run.path / 'logs/commands.json', [])
    commands.append(dict(stage=stage, time=now(), cwd=str(cwd), argv=argv))
    write_json(run.path / 'logs/commands.json', commands)
    context = adapter.begin_stage(run, stage, slot, provider, env)
    workspace = None
    try:
        actual_argv = argv
        if slot:
            from . import isolation
            workspace = isolation.IndependentWorkspace(run, stage, provider, adapter.workspace_inputs(run, stage, slot))
            before_names = set(env)
            actual_argv, env = workspace.wrap(argv, env)
            cwd = workspace.checkout
            run.manifest['removed_environment'][provider] = sorted(set(removed) | (before_names - set(env)))
            run.save()
            commands[-1].update(cwd=str(cwd), source_worktree=str(run.worktree),
                                isolation='private-snapshot-container-v1', workspace_argv=actual_argv)
            write_json(run.path / 'logs/commands.json', commands)
        result = runner(actual_argv, cwd=cwd, env=env, input_bytes=input_bytes,
                        timeout=run.runtime.model_timeout,
                        stdout_path=output, stderr_path=stderr, model_events=True, stage=stage,
                        usage_call=usage.choice(('final-review/' if hasattr(run, 'batch') else '') + stage,
                            {'provider': provider, 'model': model,
                             'effort': choice.get('effort', 'unknown') if choice else run.settings.get(provider + '_effort', 'unknown')}))
        if workspace:
            try:
                workspace.verify()
            except ApiError as exc:
                if exc.code == 'independence_failed':
                    record.update(status='failed', failure_reason=exc.code,
                                  exit_status=result.returncode, finished_at=now())
                    run.active = None
                    run.save()
                raise
            private_result = workspace.root / 'result.md'
            if result.returncode == 0 and provider == 'codex' and private_result.is_file() and not private_result.is_symlink():
                atomic_bytes(run.path / f'reviews/{stage}.md.tmp', private_result.read_bytes())
    except ApiError as exc:
        record.update(status='interrupted' if isinstance(exc, Cancelled) or exc.code == 'resource_limit' else 'failed',
                      exit_status=exc.details.get('exit_code', record.get('exit_status', 1)), finished_at=now())
        if exc.code == 'independence_failed':
            record['failure_reason'] = exc.code
        run.active = None
        if record['status'] == 'interrupted':
            adapter.interrupted(run, stage, context)
        raise
    finally:
        # Retain both private reviewer artifacts and shared-worktree artifacts
        # after the child stops, including cancellation and resource exhaustion.
        try:
            if workspace:
                try:
                    archive, generated = workspace.preserve_generated()
                    if generated:
                        record['generated_archive'] = str(archive)
                        run.manifest['generated_files'] = sorted(set(run.manifest.get('generated_files', [])) |
                            {f'{stage}: {name} (saved in {archive})' for name in generated})
                finally:
                    workspace.close()
        finally:
            try:
                adapter.end_stage(run, stage, slot, context)
            finally:
                run.save()
    record.update(exit_status=result.returncode, finished_at=now())
    if result.reason:
        record.update(failure_reason=result.reason, process_exit_status=result.process_exit_status)
    adapter.verify_snapshot(run, stage, 'after', context)
    run.save()
    if result.returncode:
        model_failure(run, stage, provider, result.returncode, output, stderr)
    # A provider can exit successfully after reporting that none of its
    # commands ran. Do not cache that diagnostic as completed analysis.
    responses = [e.get('item', {}).get('text', '') for e in result.events
                 if e.get('type') == 'item.completed' and isinstance(e.get('item'), dict)
                 and e['item'].get('type') == 'agent_message']
    responses += [json.dumps(e.get('structured_output') or e.get('result', ''))
                  for e in result.events if e.get('type') == 'result']
    if responses and providers.reported_execution_failure(responses[-1]):
        record.update(status='failed', failure_reason='sandbox_unavailable')
        run.active = None
        run.save()
        raise ApiError('model_failed', f"{stage}: model reported a sandbox launch failure "
                       "(bwrap cannot mount /proc). Review inputs could not be inspected. "
                       f"The provider environment is incompatible with outer-container execution; check managed sandbox policy and provider diagnostics before retrying. See {output}")
    return result, output
