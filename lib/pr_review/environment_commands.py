"""CLI and protocol operations for persistent repository environment profiles."""
import argparse
import json
import shutil
from pathlib import Path
import tempfile
import uuid

from . import environment, isolation, providers, settings, usage
from .common import ApiError, git, locked, read_json
from .forge import Project, restore
from .processes import run, cancellation_scope
from .review_target import resolve
from .storage import write_json


def target(request):
    from .owner_state import OwnerSession, root
    from .state import Session
    if request.get('session'):
        if Path(request['session']).resolve().is_relative_to(root()):
            session = OwnerSession(request)
            return session.repo.project, session.repo.cwd, session
        session = Session(request)
        return restore(session.manifest), Path(session.manifest['worktree']), session
    result = resolve(request)
    if not result['forge_project']:
        raise ApiError('invalid_request', 'Choose a repository with --repo.')
    return Project(**result['forge_project']), Path(request.get('cwd', '.')), None


def generate(cwd, session, request, directory):
    from . import owner_git
    ref = 'HEAD'
    if session and request.get('batch'):
        state = session.load()
        batch = selected(state, request)
        candidate = owner_git.review_commit(session, batch['checkpoint']['tree'], batch['baseline'])
        ref = candidate['ref']
    elif session:
        ref = session.manifest.get('snapshot', {}).get('head') or 'HEAD'
    sha = git(['rev-parse', '--verify', ref + '^{commit}'], cwd=cwd).stdout.strip()
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['profile_json'],
              'properties': {'profile_json': {'type': 'string'}}}
    write_json(directory / 'schema.json', schema)
    choice = dict(provider='codex', model=request.get('model') or settings.DEFAULTS['codex_model'],
                  effort=request.get('effort') or settings.DEFAULTS['codex_effort'])
    prompt = """Inspect the tracked project documentation, dependency manifests and lockfiles as data.
Return profile_json containing one JSON object with version 1 and projects keyed by short names.
Each project has directory (checkout-relative), setup (array of argv arrays), environment (string map),
prefix (argv prepended to each test), readiness (array of argv arrays run with prefix).
Use locked project tools and declared development dependencies. Put virtual environments in {scratch}
and downloads in {cache}. Only those two placeholders and {checkout} are allowed. Avoid Python f-string
placeholders in command arguments. Environment keys must be uppercase shell identifiers. Omit reserved
controller and credential variables (including PATH, HOME, TMPDIR, PYTHONPATH and all XDG_* variables).
For uv use UV_PROJECT_ENVIRONMENT={scratch}/venv, UV_CACHE_DIR={cache}, UV_LINK_MODE=copy and
uv run --no-sync as prefix. Install each package with its project manager so source imports work.
Enumerate tracked lockfiles, including nested ones, and inspect their manifests and tests. Prefer
focused development dependencies. Avoid making optional integration services a prerequisite for unit tests.
Include nested projects with documented test requirements and readiness checks that import their test dependencies.
Do not install, execute project commands, run tests, or edit files. Do not include credentials."""
    env, _ = providers.environment('codex')
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        checkout = Path(tmp) / 'checkout'
        isolation._git(tmp, 'clone', '--no-local', '--no-checkout', '--', str(cwd.resolve()), str(checkout))
        isolation._git(checkout, 'fetch', 'origin', sha)
        isolation._git(checkout, 'checkout', '--detach', sha)
        base_ref = 'refs/environment/source'
        isolation._git(checkout, 'update-ref', base_ref, sha)
        verifier = Path(tmp) / 'verification.git'
        shutil.copytree(checkout / '.git', verifier)
        argv = ['codex', 'exec', '--ephemeral', '--json', *providers.codex_settings(), '-m', choice['model'],
                '-c', 'model_reasoning_effort=' + choice['effort'], '--output-schema', str(directory / 'schema.json'),
                '-o', str(directory / 'result.json'), '-']
        lockfiles = isolation._git(checkout, 'ls-files', '-z', '*lock', '*lock.json', '*lock.yaml').stdout
        prompt += '\nReserved environment key pattern (regex search): ' + environment.RESERVED.pattern
        prompt += '\nTracked lockfile paths (repository data): ' + json.dumps([p for p in lockfiles.split('\0') if p])
        for attempt in range(2):
            output_dir = directory if attempt == 0 else directory / 'correction'
            output_dir.mkdir(exist_ok=True)
            output_path = output_dir / 'result.json'
            argv[argv.index('-o') + 1] = str(output_path)
            result = run(argv, cwd=checkout, env=env, input_bytes=prompt.encode(), timeout=providers.Runtime.environment().model_timeout,
                         model_events=True, stage='environment-generate', usage_call=usage.choice('environment/generate', choice),
                         stdout_path=output_dir / 'events.jsonl', stderr_path=output_dir / 'stderr.log')
            if result.returncode or result.reason:
                raise ApiError('environment_generation_failed', f'Codex generation failed. See {output_dir}')
            isolation.verify_checkout(checkout, verifier, sha, base_ref, sha)
            if isolation._git(checkout, 'ls-files', '--others').stdout:
                raise ApiError('integrity_failed', 'Generation created files inside the source checkout.')
            output = read_json(output_path)
            if not isinstance(output, dict) or not isinstance(output.get('profile_json'), str):
                raise ApiError('environment_generation_failed', f'Codex returned no structured profile. See {output_dir}')
            try:
                profile = environment.validate(json.loads(output['profile_json']))
                break
            except (ValueError, ApiError) as exc:
                if attempt:
                    raise ApiError('environment_generation_failed', f'Corrected profile is invalid: {exc} See {output_dir}') from exc
                from .events import emit
                emit('progress', message=f'Generated profile failed validation: {exc} Requesting one correction.')
                prompt += ('\nReturn a corrected complete profile. Validator diagnostic: ' + str(exc)
                           + '\nPrior profile (data): ' + json.dumps(output['profile_json']))
    return dict(profile=profile, source=sha, choice=choice, logs=str(directory))


def selected(state, request):
    for batch in state['batches']:
        if batch['id'] == request.get('batch'):
            return batch
    raise ApiError('invalid_request', 'Select a saved batch.')


def apply(session, profile, request):
    from .owner_state import OwnerSession
    if session is None:
        raise ApiError('invalid_request', 'Apply requires a saved session.')
    owner = isinstance(session, OwnerSession)
    with locked(session.execution_lock if owner else session.pr_lock):
        if owner:
            state = session.load()
            if request.get('revision') is not None:
                session.check(request, state)
            record = selected(state, request)
        else:
            from .state import Session
            session = Session(request)
            if session.current != session.path:
                raise ApiError('invalid_request', 'Select the current review.')
            record = session.manifest
        from . import settings
        from .owner_state import effective_settings
        previous = record.get('environment_profile')
        if not owner:
            intent = read_json(session.root / 'environment-apply.json', {})
            if intent.get('generation') == session.id:
                previous = intent['profile']
        changed = environment.identity(previous) != environment.identity(profile)
        if owner:
            config = settings.final_review_configuration(effective_settings(record['settings'])['final_review'])
        else:
            config = settings.validate(settings.from_manifest(record))
        from .review_engine import requires_independent
        stages = [slot['id'] for slot in config['reviewer_slots']] if requires_independent(config, record) else []
        stages.append('synthesis')
        if not owner and record.get('analysis_kind') == 'followup':
            stages = ['followup']
        result = dict(changed=changed, stages=stages if changed else [], profile_hash=environment.identity(profile))
        if not changed:
            return result
        if owner:
            if record.get('status') not in ('paused','interrupted','needs_attention') or record.get('commit_progress') or record.get('commit_execution'):
                raise ApiError('invalid_request', 'Apply to a stopped batch before commits begin.')
            if record.get('execution_kind') == 'issue_harness':
                from .issue_settings import validate_rerun
                validate_rerun(record, record['settings'], ['synthesis'])
            if request.get('preview'):
                return result
            record['environment_profile'] = profile
            record['environment_applied_at_remote'] = record['remote_head']
            if record.get('execution_kind') == 'issue_harness':
                from .issue_settings import request_rerun
                request_rerun(session, state, record, ['synthesis'], uuid.uuid4().hex)
            else:
                session.save(state)
        elif not request.get('preview'):
            write_json(session.root / 'environment-apply.json', dict(generation=session.id, profile=profile))
        return result


def dispatch(request):
    project, cwd, session = target(request)
    path = environment.location(project)
    action = request['command'].removeprefix('environment-')
    if action == 'show':
        profile = environment.saved(project)
        return dict(project=project.saved(), profile=profile, profile_text=json.dumps(profile, indent=2) if profile else None)
    if action == 'apply':
        profile = environment.saved(project)
        if not profile:
            raise ApiError('invalid_request', 'Generate or save a profile first.')
        if request.get('expected_hash', environment.identity(profile)) != environment.identity(profile):
            raise ApiError('stale_revision', 'Profile changed since preview.')
        return apply(session, profile, request)
    with locked(path.parent / '.lock'):
        if action == 'save':
            raw = json.loads(request['profile_text']) if 'profile_text' in request else request.get('profile')
            result = dict(profile=environment.validate(raw))
        elif action == 'generate':
            directory = path.parent / 'generations' / uuid.uuid4().hex
            directory.mkdir(parents=True, mode=0o700)
            from .operations import operation, owner
            with operation(owner(session.manifest, request['session']) if session else None):
                with usage.operation(directory, legacy=False):
                    result = generate(cwd, session, request, directory)
                result['usage'] = usage.summary(directory)
        else:
            raise ApiError('invalid_request', 'Unknown environment operation.')
        write_json(path, result)
        return result


def cli(argv):
    parser = argparse.ArgumentParser(prog='pr-review environment')
    parser.add_argument('action', choices=['show','generate','save','apply'])
    for flag in ('session','batch','cwd','model','effort','forge','file'):
        parser.add_argument('--' + flag)
    parser.add_argument('-R','--repo',dest='repository')
    parser.add_argument('--preview',action='store_true')
    args = {k:v for k,v in vars(parser.parse_args(argv)).items() if v is not None}
    args['command'] = 'environment-' + args.pop('action')
    try:
        if args.get('file'):
            args['profile'] = read_json(args['file'])
        with cancellation_scope():
            print(json.dumps(dispatch(args), indent=2))
        return 0
    except (ApiError,OSError,ValueError) as exc:
        import sys
        print(str(exc), file=sys.stderr)
        return 1
