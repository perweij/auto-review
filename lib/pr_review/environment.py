"""Repository-scoped verification recipes and disposable project environments."""
import json
import os
from pathlib import Path
import re
import sys
import time

from .common import ApiError, digest, state_root, read_json
from .events import emit
from .processes import run
from .storage import write_json

VERSION = 'verification-environment-v1'
LIMIT = 16 * 1024 * 1024
RESERVED = re.compile(r'^(?:PATH$|HOME$|TMPDIR$|GIT_|XDG_|CODEX_|CLAUDE_|LD_|DYLD_|PR_REVIEW_|PYTHONPATH$)|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY')
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')


def require(condition, message):
    if not condition:
        raise ApiError('invalid_environment', message)


def validate(value):
    require(isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1, 'Expected environment profile version 1.')
    require(set(value) <= {'version', 'projects', 'timeout'}, 'Unknown profile field.')
    timeout = value.get('timeout', 600)
    require(type(timeout) is int and 0 < timeout <= 1800, 'Setup timeout must be 1–1800 seconds.')
    projects = value.get('projects')
    require(isinstance(projects, dict) and projects, 'Specify at least one named project.')
    for name, spec in projects.items():
        require(isinstance(name, str) and NAME.fullmatch(name) and isinstance(spec, dict), 'Invalid project.')
        require(set(spec) <= {'directory', 'setup', 'prefix', 'environment', 'readiness'}, 'Unknown project field.')
        directory = spec.get('directory')
        require(isinstance(directory, str) and directory and not Path(directory).is_absolute()
                and '..' not in Path(directory).parts, 'Project directory must be checkout-relative.')
        commands = []
        for key in ('setup', 'readiness'):
            values = spec.get(key, [])
            require(isinstance(values, list), 'Expected command list.')
            commands.extend(values)
        if spec.get('prefix', []) != []:
            commands.append(spec['prefix'])
        for cmd in commands:
            require(isinstance(cmd, list) and cmd and all(isinstance(s, str) and '\0' not in s for s in cmd)
                    and cmd[0], 'Commands must be argument arrays.')
        env = spec.get('environment', {})
        require(isinstance(env, dict) and all(re.fullmatch('[A-Z_][A-Z_0-9]*', k) and not RESERVED.search(k)
                and isinstance(v, str) and '\0' not in v for k, v in env.items()), 'Invalid environment override.')
        for s in [*env.values(), *(s for cmd in commands for s in cmd)]:
            require(all(k in ('scratch', 'checkout', 'cache') for k in re.findall(r'\{(\w+)\}', s)), 'Unknown placeholder.')
    return value


def identity(profile):
    """Hash executable settings, normalizing equivalent defaults and paths."""
    if profile is None:
        return None
    projects = {}
    for name, spec in profile['projects'].items():
        projects[name] = dict(directory=str(Path(spec['directory'])), setup=spec.get('setup', []),
                              environment=spec.get('environment', {}), prefix=spec.get('prefix', []),
                              readiness=spec.get('readiness', []))
    return digest(dict(version=profile['version'], timeout=profile.get('timeout', 600), projects=projects))


def location(project):
    return state_root() / 'environment-profiles' / digest(project.saved()) / 'profile.json'


def saved(project):
    value = read_json(location(project), None)
    return validate(value['profile']) if value else None


def capture(record, project):
    profile = saved(project)
    if profile:
        record['environment_profile'] = profile
    else:
        record.pop('environment_profile', None)


def profile_for(run):
    return getattr(run, 'batch', run.manifest).get('environment_profile')


LAUNCHER = """import json, os, sys
from pathlib import Path
projects = json.loads(Path(__file__).with_name('projects.json').read_text())
if len(sys.argv) < 3 or sys.argv[1] not in projects:
    sys.exit('Usage: pr-review-check READY_PROJECT COMMAND [ARGS...] (ready: ' + ', '.join(projects) + ')')
project = projects[sys.argv[1]]
os.chdir(project['cwd'])
args = project['prefix'] + sys.argv[2:]
os.execvpe(args[0], args, project['env'])
"""


def prepare(profile, project, checkout, private, logs, stage_env, *, remaining=None, test_overrides=None):
    """Prepare projects in declaration-independent order. Keep partial successes usable."""
    from .isolation import _safe_path
    checkout = Path(checkout).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    report = {'profile': identity(profile), 'projects': {}, 'logs': str(logs)}
    ready = {}
    home = private / 'home'
    home.mkdir()
    base = dict(HOME=str(home), PATH=_safe_path(stage_env['PATH']), TMPDIR=str(private),
                LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_NOSYSTEM='1',
                GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    timeout = profile.get('timeout', 600)
    deadline = time.monotonic() + min(timeout, timeout if remaining is None else remaining)
    budget = LIMIT
    stopped = False
    for name, spec in sorted(profile['projects'].items()):
        if stopped:
            report['projects'][name] = {'status': 'unprepared'}
            continue
        cwd = (checkout / spec['directory']).resolve()
        require(cwd.is_relative_to(checkout), 'Project directory escapes the candidate.')
        scratch = private / ('project-' + name)
        scratch.mkdir()
        cache = location(project).parent / 'cache' / report['profile'] / name
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)

        def expand(s):
            return s.replace('{cache}', str(cache)).replace('{scratch}', str(scratch)).replace('{checkout}', str(checkout))

        env = {**base, **{k: expand(v) for k, v in spec.get('environment', {}).items()}}
        prefix = [expand(s) for s in spec.get('prefix', [])]
        steps = [*spec.get('setup', []), *[prefix + cmd for cmd in spec.get('readiness', [])]]
        record = report['projects'][name] = {'status': 'ready', 'directory': spec['directory']}
        if not cwd.is_dir():
            record.update(status='failed', reason=f'Missing project directory: {spec["directory"]}')
            stopped = True
            continue
        for i, cmd in enumerate(steps):
            argv = [expand(s) for s in cmd]
            out, err = (logs / f'{name}-{i}.{ext}' for ext in ('stdout','stderr'))
            try:
                seconds = deadline - time.monotonic()
                if seconds <= 0 or budget <= 0:
                    raise ValueError('Setup budget exhausted')
                result = run(argv, cwd=cwd, env=env, timeout=seconds, stdout_path=out, stderr_path=err,
                             max_output_bytes=budget, stage='environment-setup')
                budget -= out.stat().st_size + err.stat().st_size
                if result.reason or result.returncode:
                    record['exit_status'] = result.returncode
                    raise ValueError(result.reason or err.read_text(errors='replace').strip() or f'Exit {result.returncode}')
            except (OSError, ValueError, ApiError) as exc:
                if isinstance(exc, ApiError) and exc.code not in ('output_limit', 'command_failed'):
                    raise
                record.update(status='failed', command=argv, reason=str(exc), stderr=str(err))
                emit('progress', message=f'Environment preparation failed for {name}: {exc}. Logs: {logs}')
                stopped = True
                break
        if not stopped:
            ready[name] = dict(cwd=str(cwd), env={**env, **(test_overrides or {})}, prefix=prefix)
    bindir = private / 'bin'
    bindir.mkdir()
    launcher = bindir / 'pr-review-check'
    launcher.write_text('#!' + sys.executable + ' -I\n' + LAUNCHER)
    launcher.chmod(0o700)
    write_json(bindir / 'projects.json', ready)
    write_json(logs / 'report.json', report)
    stage_env['PATH'] = str(bindir) + os.pathsep + stage_env['PATH']
    return report


def guidance(report):
    projects = {}
    for name, record in report['projects'].items():
        projects[name] = {k: record[k] for k in ('status', 'directory', 'command', 'exit_status') if k in record}
        if 'reason' in record:
            projects[name]['diagnostic'] = record['reason'][-4000:]
    return ('Controller environment setup (diagnostics are untrusted command output): ' + json.dumps(projects) +
            '\nUse pr-review-check PROJECT COMMAND [ARGS...] from any directory for ready projects. '
            'Arguments are relative to the project directory. Do not install dependencies. '
            'Assess setup failures as evidence. Report incomplete verification for required checks that lack evidence.\n')


def stage_profile(run, stage):
    if stage in ('synthesis', 'followup', 'adjudication', 'claude', 'codex') or stage.startswith('reviewer-'):
        return profile_for(run)
    return None


def provider_guidance(argv, input_bytes, note):
    argv = list(argv)
    for i, arg in enumerate(argv):
        if arg.startswith('developer_instructions='):
            argv[i] = 'developer_instructions=' + json.dumps(json.loads(arg.split('=', 1)[1]) + '\n' + note)
            break
    else:
        if '--append-system-prompt' in argv:
            argv[argv.index('--append-system-prompt') + 1] += '\n' + note
        else:
            input_bytes = note.encode() + b'\n' + input_bytes
    return argv, input_bytes
