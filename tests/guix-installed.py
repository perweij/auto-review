"""Offline checks of the built backend, invoked with its own Python interpreter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROBE = """
import json, os, runpy, shutil, sys
runpy.run_path(sys.argv[1], run_name='packaging_probe')
from pr_review.common import ROOT, state_root
from pr_review.providers import review_command, Runtime
from pr_review.settings import DEFAULTS
from pathlib import Path
settings = dict(DEFAULTS, processing_provider='codex')
args = review_command(settings, Runtime(), 'contributions', Path('relative session'), 'base')
schema = Path(args[args.index('--output-schema') + 1])
assert schema.is_absolute() and json.loads(schema.read_text())['type'] == 'object'
args = review_command(DEFAULTS, Runtime(), 'synthesis', Path('relative session'), 'base')
assert args[args.index('--json-schema') + 1] == (ROOT / 'prompts/synthesis.schema.json').read_text().rstrip('\\n')
assert args[args.index('--append-system-prompt') + 1] == (ROOT / 'prompts/synthesis-testing-focused.txt').read_text().rstrip('\\n')
assert 'relative session/context' in args
assert (ROOT / 'prompts/threads.graphql').is_file()
print(json.dumps({
    'root': str(ROOT), 'state': str(state_root()), 'python': sys.executable,
    'tools': {name: shutil.which(name) for name in ('git', 'gh', 'ssh', 'claude', 'codex', 'gpg', 'vi')},
    'env': {name: os.environ.get(name) for name in (
        'PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HOME', 'EDITOR', 'VISUAL',
        'SSH_AUTH_SOCK', 'GNUPGHOME', 'GH_CONFIG_DIR')}
}))
"""


def check_backend(program):
    program = program.resolve()
    assert str(program).startswith('/gnu/store/'), program
    installation = program.parent.parent
    assert str(Path(sys.executable).resolve()).startswith('/gnu/store/')
    before = {str(p.relative_to(installation)): p.read_bytes()
              for p in installation.rglob('*') if p.is_file()}
    with tempfile.TemporaryDirectory(prefix='installed backend ') as temporary:
        root = Path(temporary)
        caller, host = root / 'unrelated caller', root / 'host tools'
        caller.mkdir()
        host.mkdir()
        # Host integrations must remain discoverable; packaged tools must win.
        for name in ('git', 'gh', 'ssh', 'claude', 'codex', 'gpg', 'vi'):
            tool = host / name
            tool.write_text('#!/bin/sh\nexit 97\n')
            tool.chmod(0o755)
        link = host / 'pr-review'
        link.symlink_to(program)
        env = {'PATH': str(host), 'LC_ALL': 'C', 'HOME': str(root / 'home'),
               'XDG_STATE_HOME': str(root / 'state'), 'XDG_CONFIG_HOME': str(root / 'config'),
               'XDG_CACHE_HOME': str(root / 'cache'), 'EDITOR': 'my editor',
               'VISUAL': 'my visual editor', 'SSH_AUTH_SOCK': str(root / 'ssh socket'),
               'GNUPGHOME': str(root / 'gnupg'), 'GH_CONFIG_DIR': str(root / 'gh')}

        def invoke(args, environment=env, data=None):
            result = subprocess.run(args, cwd=caller, env=environment, input=data,
                                    text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, (args, result.stdout, result.stderr)
            return result.stdout

        assert '--resume' in invoke([str(link), '--help'])
        assert '--auto-commit' in invoke([str(link), 'owner', '--help'])
        result = json.loads(invoke([str(link), 'api'],
                                  data='{"protocol":1,"command":"capabilities"}\n'))
        assert result['result']['protocol'] == 1
        assert 'owner-fix' in result['result']['commands']

        observed = json.loads(invoke([sys.executable, '-c', PROBE, str(link)]))
        assert observed['root'] == str(installation)
        assert observed['state'] == str(root / 'state/pr-review')
        assert Path(observed['python']).resolve() == Path(sys.executable).resolve()
        for name in ('git', 'gh', 'ssh'):
            assert observed['tools'][name].startswith('/gnu/store/'), observed
        for name in ('claude', 'codex', 'gpg', 'vi'):
            assert observed['tools'][name] == str(host / name), observed
        for name in ('HOME', 'EDITOR', 'VISUAL', 'SSH_AUTH_SOCK', 'GNUPGHOME', 'GH_CONFIG_DIR'):
            assert observed['env'][name] == env[name], (name, observed)
        for name in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
            assert observed['env'][name].startswith('/gnu/store/'), observed
            assert Path(observed['env'][name]).exists(), (name, observed['env'][name])
        bundle = Path(observed['env']['SSL_CERT_FILE'])
        assert bundle.is_file() and installation in bundle.parents, bundle
        assert Path(observed['env']['SSL_CERT_DIR']).is_dir(), observed

        custom = {**env, 'SSL_CERT_FILE': str(root / 'custom CA.pem'),
                  'SSL_CERT_DIR': str(root / 'custom certificates')}
        overridden = json.loads(invoke([sys.executable, '-c', PROBE, str(link)], custom))
        for name in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
            assert overridden['env'][name] == custom[name], overridden
        # Backend startup must not create state.  Tools checked below may create
        # their own state (for example, GitHub CLI's device identifier).
        assert not (root / 'state').exists()

        configured = {**env, **observed['env']}
        invoke([observed['tools']['git'], '--version'], configured)
        invoke([observed['tools']['ssh'], '-V'], configured)
        help_text = invoke([observed['tools']['gh'], 'api', '--help'], configured)
        assert all(flag in help_text for flag in ('--slurp', '--paginate', '--hostname'))
        invoke([sys.executable, '-c',
                'import os, ssl; '
                'context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); '
                'context.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"]); '
                'assert context.cert_store_stats()["x509_ca"] > 0; '
                'assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0'],
               configured)

        assert not list(caller.iterdir())
    after = {str(p.relative_to(installation)): p.read_bytes()
             for p in installation.rglob('*') if p.is_file()}
    assert before == after, 'Execution changed the installation'
    assert not list(installation.rglob('__pycache__'))
    print('Installed backend: protocol, resources, runtime inputs, TLS and host environment passed.')


if __name__ == '__main__':
    check_backend(Path(sys.argv[1]))
