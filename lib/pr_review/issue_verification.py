"""Bounded controller execution of checks explicitly named in an approved issue plan.

This is separate from model verification policy. A fresh candidate checkout binds
launch-time source to a Git tree; before/after integrity checks reject modifications.
It uses the host development environment, not a security sandbox or CI attestation.
"""
import copy
import os
from pathlib import Path
import re
import shlex
import time
from types import SimpleNamespace
import uuid

from .common import ApiError, now
from .events import emit
from . import isolation, owner_git, processes, specification, test_evidence
from .review_workspace import CandidateReview, verify_live
from .storage import hash_bytes, write_json

MAX_COMMANDS = 4
MAX_SECONDS = 1800
MAX_LOG_BYTES = 16 * 1024 * 1024


def parse_command(command):
    """A small argv-only test command grammar: no shell, pipelines or expansion."""
    if not isinstance(command, str) or len(command) > 8192 or any(c in command for c in '\n\r;|&<>`$'):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    environment = {}
    while args and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*', args[0]):
        name, value = args.pop(0).split('=', 1)
        if name not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPATH', 'PYTHONHASHSEED', 'LC_ALL', 'LANG'):
            return None
        if name == 'PYTHONPATH' and any(Path(p).is_absolute() or '..' in Path(p).parts for p in value.split(':')):
            return None
        environment[name] = value
    if not args or any('smoke' in a.lower() for a in args):
        return None
    program = args[0]
    if re.fullmatch(r'python(?:3(?:\.\d+)?)?', program):
        valid = len(args) >= 3 and args[1] == '-m' and args[2] in ('unittest', 'pytest')
    elif program == 'pytest':
        valid = True
    elif program == 'make':
        valid = len(args) > 1 and all(re.fullmatch(r'(?:check|test|lint|verify)(?:[-_][A-Za-z0-9_-]+)?', a) for a in args[1:])
    else:
        valid = False
    return (args, environment) if valid else None


def approved_commands(session, state):
    text = specification.handoff(session, state)['text']
    # Only test sections or explicit run instructions authorize collection, not
    # incidental examples elsewhere in the specification. Negative/optional
    # instructions are conservatively excluded rather than interpreted by a model.
    result = set()
    testing = fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        heading = re.match(r'^#{1,6}\s+(.+)', stripped)
        if heading and not fenced:
            title = heading[1].lower()
            testing = bool(re.search(r'\b(?:verification|validation|testing|tests)\b', title))
            testing = testing and not re.search(r'\b(?:optional|excluded|unsupported)\b', title)
        if stripped.startswith('```'):
            fenced = not fenced
            continue
        if fenced:
            if testing and parse_command(stripped):
                result.add(stripped)
            continue
        # Hide code spans before splitting prose: dotted module names, quoted
        # punctuation and pytest's "not" expressions are command data, not prose.
        snippets = []
        def hide_code(match):
            snippets.append(match[1])
            return f'\x00{len(snippets) - 1}\x00'
        prose = re.sub(r'(?<!`)`([^`\n]+)`(?!`)', hide_code, stripped)
        for sentence in re.split(r'[.!?](?:\s+|$)', prose):
            if re.search(r"\b(?:not|never|skip|avoid|optional|forbidden)\b|don't", sentence, re.I):
                continue
            instruction = re.sub(r'^(?:[-*]|[0-9]+[.)])\s+', '', sentence.strip())
            explicit = re.match(r'^(?:run|execute)\b', instruction, re.I)
            if not testing and not explicit:
                continue
            for number in re.findall(r'\x00([0-9]+)\x00', sentence):
                command = snippets[int(number)].strip()
                if parse_command(command):
                    result.add(command)
    return result


def references(batch):
    return [copy.deepcopy(r['reference']) for r in batch.get('verification_runs', []) if r.get('reference')]


def _read(path, limit=MAX_LOG_BYTES):
    path = Path(path)
    return test_evidence._read(path.parent, path.name, limit)


def index(batch):
    """Check frozen receipts and log bytes before exposing them to synthesis."""
    import json
    result = []
    for ref in references(batch):
        try:
            raw = _read(ref['manifest'])
            if hash_bytes(raw) != ref['sha256']:
                raise ValueError('manifest hash changed')
            value = json.loads(raw)
            for name in ('stdout', 'stderr'):
                artifact = value.get(name)
                if artifact:
                    data = _read(Path(ref['manifest']).parent / artifact['path'])
                    if hash_bytes(data) != artifact['sha256'] or len(data) != artifact['bytes']:
                        raise ValueError('log bytes changed')
                    artifact['path'] = str(Path(ref['manifest']).parent / artifact['path'])
            result.append({**value, 'manifest': ref['manifest'], 'sha256': ref['sha256']})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ApiError('test_evidence_integrity', f'Controller verification evidence changed: {exc}') from exc
    return result


def _finish(session, state, record, value):
    directory = Path(record['path'])
    for name in ('stdout', 'stderr'):
        path = directory / (name + '.log')
        if path.exists():
            try:
                raw = _read(path)
                value[name] = {'path': path.name, 'sha256': hash_bytes(raw), 'bytes': len(raw)}
            except (OSError, ValueError) as exc:
                value.update(status='unavailable', error=str(exc), source_check='unknown')
        elif value['status'] == 'completed':
            value.update(status='unavailable', error=f'Missing {name} log', source_check='unknown')
    path = directory / 'result.json'
    value['finished_at'] = now()
    write_json(path, value)
    record.update(status=value['status'], elapsed_seconds=value['elapsed_seconds'],
                  reference={'manifest': str(path), 'sha256': hash_bytes(_read(path))})
    session.save(state)


def recover(session, state, batch):
    """Publish interrupted intents before synthesis can consider acceptance."""
    changed = False
    for record in batch.get('verification_runs', []):
        if record['status'] == 'running':
            _finish(session, state, record, {**record['input'], 'status': 'interrupted',
                'exit_code': None, 'source_check': 'unknown', 'elapsed_seconds': record['timeout'],
                'error': 'Execution was interrupted before its result was saved; not automatically retried.'})
            changed = True
    return changed


def collect(session, state, batch, blockers):
    """Collect new eligible evidence once, returning whether synthesis should rerun."""
    eligible = approved_commands(session, state)
    tree = batch['checkpoint']['tree']
    records = batch.setdefault('verification_runs', [])
    changed = recover(session, state, batch)
    index(batch)
    epoch = batch.get('review_reruns', {}).get('synthesis')
    current = [r for r in records if r['tree'] == tree and r.get('epoch') == epoch]
    if any(r['status'] != 'completed' for r in current):
        return changed  # Assess the known failure before launching more suites.
    commands = list(dict.fromkeys(b['command'] for b in blockers
                    if b.get('category') in ('evidence', 'policy') and b.get('command') in eligible))
    for command in commands:
        if any(r['command'] == command for r in current) or len(current) >= MAX_COMMANDS:
            continue
        remaining = MAX_SECONDS - sum(r.get('elapsed_seconds', r.get('timeout', 0)) for r in current)
        if remaining <= 0:
            break
        args, overrides = parse_command(command)
        candidate = owner_git.review_commit(session, tree, batch['baseline'])
        candidate['baseline_ref'] = batch.get('baseline_ref') or owner_git.pin_commit(session, batch['baseline'])
        directory = session.path / 'verification' / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'tmp').mkdir()
        run = CandidateReview(session, state, batch, SimpleNamespace(name='issue-verification'),
                              directory, candidate, {}, None, {})
        stage = 'check'
        value = {'format_version': 1, 'epoch': epoch, 'command': command, 'argv': args, 'environment': overrides,
                 'specification': specification.handoff(session, state)['locator'],
                 'candidate': candidate, 'cwd': str(run.checkout_path(stage)), 'started_at': now(),
                 'status': 'running', 'source_check': 'unknown', 'exit_code': None,
                 'limitations': 'Controller-selected candidate checkout; integrity checked before and after execution. '
                                'Tests run on the host, not in a security sandbox; transient self-modification is not attested.'}
        record = {'tree': tree, 'epoch': epoch, 'command': command, 'path': str(directory), 'input': value,
                  'status': 'running', 'timeout': remaining}
        records.append(record)
        current.append(record)
        session.save(state)  # Durable intent precedes any test process.
        started = time.monotonic()
        raised = None
        try:
            verify_live(run, 'before')
            run.create_checkout(stage)
            run.verify_checkout(stage, 'before')
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')
                   and not re.search(r'TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY(?:_|$)', k.upper())}
            env.update(TMPDIR=str(run.stage_dir(stage) / 'scratch'), PYTHONDONTWRITEBYTECODE='1',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
            env.update(overrides)
            env['PATH'] = isolation._safe_path(env.get('PATH', ''))
            argv = [isolation._executable(args[0], env['PATH']), *args[1:]]
            value['argv'] = argv
            emit('progress', message=f'Collecting verification evidence for candidate {tree[:12]}: {command}')
            result = processes.run(argv, cwd=run.checkout_path(stage), env=env, timeout=max(0, remaining - (time.monotonic() - started)),
                                   stdout_path=directory / 'stdout.log', stderr_path=directory / 'stderr.log',
                                   max_output_bytes=MAX_LOG_BYTES, stage='issue-verification')
            value.update(exit_code=result.returncode,
                         status='timeout' if result.reason == 'timeout' else 'completed' if result.returncode == 0 else 'failed')
            run.verify_checkout(stage, 'after')
            value['source_check'] = 'verified'
        except (ApiError, OSError) as exc:
            value.update(status='interrupted' if isinstance(exc, processes.Cancelled) else 'unavailable', error=str(exc))
            if getattr(exc, 'code', '') in ('review_checkout_modified', 'integrity_failed', 'worktree_changed'):
                value['source_check'] = 'failed'
            if getattr(exc, 'code', '') in ('cancelled', 'resource_limit', 'integrity_failed', 'worktree_changed'):
                raised = exc
        finally:
            try:
                verify_live(run, 'after')
            except ApiError as exc:
                if exc.code in ('cancelled', 'resource_limit'):
                    value.update(status='interrupted', source_check='unknown', error=str(exc))
                else:
                    value.update(status='unavailable', source_check='failed', error=str(exc))
                raised = exc
            value['elapsed_seconds'] = time.monotonic() - started
            try:
                _finish(session, state, record, value)
            finally:
                run.close()
        changed = True
        if raised:
            raise raised
        if value['status'] != 'completed':
            break
    return changed
