"""Relocated runtime modules and assets, with no installation writes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_symlink_loads_modules_and_assets_without_dirtying_checkout(self):
        with tempfile.TemporaryDirectory(prefix='packaging smoke ') as temporary:
            root = Path(temporary)
            checkout = root / 'relocated installation'
            for name in ('bin', 'lib', 'prompts'):
                shutil.copytree(ROOT / name, checkout / name,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            caller, path = root / 'caller directory', root / 'command links'
            caller.mkdir()
            path.mkdir()
            link = path / 'pr-review'
            link.symlink_to(checkout / 'bin/pr-review')
            (path / 'python3').symlink_to(sys.executable)
            env = {'PATH': str(path), 'HOME': str(root / 'home'), 'LC_ALL': 'C.UTF-8'}
            for variable in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
                env.setdefault(variable, str(root / variable.lower()))
                Path(env[variable]).mkdir(exist_ok=True, mode=0o700)

            def files():
                return {str(p.relative_to(checkout)): (p.stat().st_mode, p.read_bytes())
                        for p in checkout.rglob('*') if p.is_file()}

            before = files()
            def invoke(args, data=None):
                result = subprocess.run(args, cwd=caller, env=env, input=data,
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            self.assertIn('--resume', invoke([str(link), '--help']))
            self.assertIn('--auto-commit', invoke([str(link), 'owner', '--help']))
            result = json.loads(invoke([str(link), 'api'], '{"protocol":1,"command":"capabilities"}\n'))
            self.assertIn('owner-fix', result['result']['commands'])
            # Load through the same symlink, then exercise real asset consumers
            # without invoking a provider or introducing a special CLI command.
            code = '''
import json, runpy, sys
from pathlib import Path
sys.argv = [sys.argv[1], "--help"]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0
from pr_review.common import ROOT
from pr_review.providers import review_command, Runtime
from pr_review.settings import DEFAULTS
args = review_command(DEFAULTS, Runtime(), "synthesis", Path("relative session"), "base")
assert args[args.index("--json-schema") + 1] == (ROOT / "prompts/synthesis.schema.json").read_text().rstrip("\\n")
assert args[args.index("--append-system-prompt") + 1] == (ROOT / "prompts/synthesis-testing-focused.txt").read_text().rstrip("\\n")
assert "relative session/context" in args
settings = dict(DEFAULTS, processing_provider="codex")
args = review_command(settings, Runtime(), "contributions", Path("relative session"), "base")
schema = Path(args[args.index("--output-schema") + 1])
assert schema.is_absolute() and json.loads(schema.read_text())["type"] == "object"
'''
            invoke([str(path / 'python3'), '-c', code, str(link)])
            self.assertEqual(files(), before)
            self.assertFalse(list(checkout.rglob('__pycache__')))
            self.assertFalse(list(caller.iterdir()))
            for variable in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR'):
                self.assertFalse(list(Path(env[variable]).iterdir()), variable)


if __name__ == '__main__':
    unittest.main()
