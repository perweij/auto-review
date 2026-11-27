"""Contracts for portable checkout preparation and profile-aware launchers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from pr_review import environment
from pr_review.common import ApiError
from pr_review.review_target import project_for


class VerificationEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / 'clone'
        self.quality = self.checkout / 'tools/quality'
        self.quality.mkdir(parents=True)
        self.env = dict(os.environ)
        patched = patch.dict(os.environ, XDG_STATE_HOME=str(self.root / 'state'))
        patched.start()
        self.addCleanup(patched.stop)
        self.project = project_for('github.com/Example/Repo')
        self.spec = {'directory': 'tools/quality', 'prefix': [sys.executable, '-I'],
                     'environment': {'TEST_DIR': '{scratch}'}}
        self.profile = {'version': 1, 'projects': {'quality': self.spec}}

    def prepare(self):
        private = self.root / 'private'
        private.mkdir()
        return environment.prepare(environment.validate(self.profile), self.project, self.checkout,
                                   private, self.root / 'logs', self.env)

    def test_prepare_and_launch_from_different_working_directory(self):
        self.spec['setup'] = [[sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', '{scratch}/installed']]
        self.spec['readiness'] = [['-c', 'import os,pathlib; assert (pathlib.Path(os.environ["TEST_DIR"])/"installed").exists()']]
        report = self.prepare()
        self.assertEqual(report['projects']['quality']['status'], 'ready')
        result = subprocess.check_output(['pr-review-check','quality','-c','import os; print(os.getcwd())'],
                                         cwd=self.root, env=self.env, text=True)
        self.assertEqual(result.strip(), str(self.quality))
        self.assertFalse(list(self.quality.iterdir()))

    def test_run_selected_checks_across_candidate_projects(self):
        from pr_review.environment_commands import run_checks
        self.profile['projects'].update(root=dict(directory='.'), unused=dict(directory='absent'))
        checks = [dict(project='quality', argv=['-c', 'import os; assert os.getcwd().endswith("tools/quality")'],
                       reason='quality cwd'), dict(project='root', argv=[sys.executable, '-c',
                       'from pathlib import Path; assert Path("tools/quality").is_dir()'], reason='root cwd')]
        request = self.root / 'request.json'
        logs = self.root / 'logs'
        request.write_text(json.dumps(dict(project=self.project.saved(), profile=self.profile, checks=checks)))
        self.assertEqual(run_checks(['--cwd', str(self.checkout), '--file', str(request), '--logs', str(logs)]), 0)
        self.assertEqual(set(json.loads((logs / 'report.json').read_text())['projects']), {'quality', 'root'})
        self.assertEqual(json.loads((logs / 'check-1.json').read_text())['exit_status'], 0)

    def test_run_checks_reports_unavailable_required_project(self):
        from pr_review.environment_commands import cli
        self.spec['readiness'] = [['-c', 'raise SystemExit(1)']]
        request = self.root / 'request.json'
        logs = self.root / 'logs'
        request.write_text(json.dumps(dict(project=self.project.saved(), profile=self.profile,
                                           checks=[dict(project='quality', argv=['-V'], reason='test')])))
        self.assertEqual(cli(['run-checks', '--cwd', str(self.checkout), '--file', str(request),
                              '--logs', str(logs)]), 1)
        self.assertFalse((logs / 'check-0.json').exists())

    def container_bin(self, *tools):
        bindir = self.root / 'container-tools'
        bindir.mkdir()
        for tool in tools:
            script = bindir / tool
            script.write_text(f'#!/bin/sh\nprintf "{tool}|%s|%s|%s\\n" "$HOME" "$XDG_RUNTIME_DIR" "$*"\n')
            script.chmod(0o755)
        self.env.update(PATH=str(bindir), HOME='/home/controller test', XDG_RUNTIME_DIR='/run/user/4242')
        self.spec.pop('prefix')

    def launch(self, *argv):
        return subprocess.run(['pr-review-check', 'quality', *argv], env=self.env, capture_output=True,
                              text=True).stdout.strip()

    def test_podman_provides_docker_with_controller_container_state(self):
        self.container_bin('podman')
        report = self.prepare()
        self.assertEqual(report['container_cli']['docker'], 'podman')
        self.assertEqual(self.launch('docker', 'run', '--rm', 'image'),
                         'podman|/home/controller test|/run/user/4242|run --rm image')
        self.assertEqual(json.loads((self.root / 'logs/report.json').read_text())['container_cli'],
                         report['container_cli'])

    def test_installed_docker_or_missing_podman_leaves_path_unchanged(self):
        self.container_bin('docker', 'podman')
        self.assertNotIn('container_cli', self.prepare())
        # A real Docker CLI keeps the disposable HOME; only the Podman shim sees the controller's.
        self.assertEqual(self.launch('docker', 'ps'), f'docker|{self.root / "private/home"}||ps')
        path, record = environment.container_cli(str(self.root / 'absent'), self.env, self.root / 'unused')
        self.assertEqual((path, record), (str(self.root / 'absent'), None))
        self.assertFalse((self.root / 'unused').exists())

    def test_setup_failure_retains_reason_and_disables_project(self):
        self.spec['readiness'] = [['-c', 'raise RuntimeError("dependency missing")']]
        self.profile['projects']['z'] = dict(directory='.')
        report = self.prepare()
        self.assertIn('dependency missing', report['projects']['quality']['reason'])
        self.assertEqual(report['projects']['z']['status'], 'ready')
        self.assertEqual(list(json.loads((self.root/'private/bin/projects.json').read_text())), ['z'])

    @unittest.skipUnless(shutil.which('uv'), 'requires uv')
    def test_same_profile_installs_dependencies_of_each_reviewed_revision(self):
        self.profile['projects']['quality'] = {'directory': 'tools/quality', 'uv_sync': {'groups': ['dev']}}
        # Local wheel and offline lock generation keep both revisions self-contained.
        wheel = self.quality / 'probe_dep-1.0-py3-none-any.whl'
        metadata = 'probe_dep-1.0.dist-info/'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('probe_dep.py', 'VALUE = 42')
            archive.writestr(metadata + 'METADATA', 'Metadata-Version: 2.1\nName: probe-dep\nVersion: 1.0\n')
            archive.writestr(metadata + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr(metadata + 'RECORD', '')
        for revision, deps in enumerate(('["probe-dep"]', '[]')):
            (self.quality / 'pyproject.toml').write_text(
                '[project]\nname="quality"\nversion="1"\nrequires-python=">=3.9"\n'
                f'[dependency-groups]\ndev={deps}\n'
                f'[tool.uv.sources]\nprobe-dep={{path="{wheel.name}"}}\n')
            subprocess.run(['uv', 'lock', '--offline', '--python', sys.executable], cwd=self.quality, check=True)
            private = self.root / str(revision)
            private.mkdir()
            env = dict(self.env)
            report = environment.prepare(self.profile, self.project, self.checkout, private, private / 'logs', env)
            self.assertEqual(report['projects']['quality']['status'], 'ready', report)
            subprocess.run(['pr-review-check', 'quality', 'python', '-c',
                            f'import importlib.util; assert bool(importlib.util.find_spec("probe_dep")) == {revision == 0}'],
                           env=env, check=True)
        self.assertFalse((self.quality / '.venv').exists())

    def test_uv_install_flags_and_invalid_recipes(self):
        spec = {'directory': '.', 'uv_sync': {'groups': 'all', 'extras': ['search-sync']}}
        self.assertEqual(environment.uv_project(spec)['setup'],
                         [['uv', 'sync', '--locked', '--all-groups', '--extra', 'search-sync']])
        for change in ({'prefix': ['python']}, {'uv_sync': {'extras': 'all'}},
                       {'uv_sync': {'groups': ['--offline']}}, {'uv_sync': {'lock': False}}):
            with self.subTest(change=change), self.assertRaises(ApiError):
                environment.validate({'version': 1, 'projects': {'root': {**spec, **change}}})

    def test_uv_project_without_own_lock_cannot_use_parent(self):
        (self.checkout / 'uv.lock').touch()
        (self.quality / 'pyproject.toml').touch()
        self.profile['projects']['quality'] = {'directory': 'tools/quality', 'uv_sync': {}}
        result = self.prepare()['projects']['quality']
        self.assertEqual(result['status'], 'failed')
        self.assertIn('uv.lock', result['reason'])

    def test_missing_setup_executable_leaves_review_available(self):
        self.spec['setup'] = [['absent-environment-bootstrap-fixture']]
        project = self.prepare()['projects']['quality']
        self.assertEqual(project['status'], 'failed')
        self.assertIn('absent-environment-bootstrap-fixture', project['reason'])

    def test_symlink_escape_and_reserved_override(self):
        self.spec['environment']['PATH'] = '{checkout}'
        with self.assertRaises(ApiError):
            environment.validate(self.profile)
        del self.spec['environment']['PATH']
        (self.checkout/'outside').symlink_to(self.root)
        self.spec['directory'] = 'outside'
        with self.assertRaisesRegex(ApiError, 'escapes'):
            self.prepare()

    def test_output_and_time_limits_leave_diagnostics(self):
        from time import monotonic
        self.spec['setup'] = [[sys.executable, '-c', 'print("x" * 12000)']]
        self.profile['projects']['z'] = dict(directory='.')
        with patch.object(environment, 'LIMIT', 4096):
            result = self.prepare()
        self.assertEqual(result['projects']['quality']['status'], 'failed')
        self.assertIn('limit', result['projects']['quality']['reason'])
        self.assertEqual(result['projects']['z']['status'], 'unprepared')
        self.profile['timeout'] = 1
        self.spec['setup'] = [[sys.executable, '-c', 'import time; time.sleep(30)']]
        other = self.root / 'other'
        other.mkdir()
        start = monotonic()
        result = environment.prepare(self.profile, self.project, self.checkout, other,
                                     self.root/'second-logs', self.env)
        self.assertLess(monotonic() - start, 10)
        self.assertEqual(result['projects']['quality']['reason'], 'timeout')

    def test_repository_identity_and_semantic_defaults(self):
        other = project_for('github.com/EXAMPLE/REPO')
        self.assertEqual(environment.location(self.project), environment.location(other))
        self.assertNotEqual(environment.location(other), environment.location(project_for('gitlab.com/example/repo')))
        explicit = json.loads(json.dumps(self.profile))
        explicit.update(timeout=600)
        explicit['projects']['quality'].update(readiness=[], setup=[])
        self.assertEqual(environment.identity(self.profile), environment.identity(explicit))


from engine_fixture import EngineFixture


class ProfiledReviewTest(EngineFixture):
    def test_prepares_each_review_checkout_and_reuses_results(self):
        self.batch['environment_profile'] = {'version': 1, 'projects': {'root': {
            'directory': '.', 'readiness': [[sys.executable, '-c',
                'from pathlib import Path; assert "local edit" in Path("source.txt").read_text()']]}}}
        self.session.save(self.state)
        self.review()
        phases = self.manifest()['phases']
        for stage in ('reviewer-1', 'reviewer-2', 'synthesis'):
            self.assertEqual(phases[stage]['environment']['projects']['root']['status'], 'ready')
            self.assertIn('environment', phases[stage]['inputs'])
        calls = self.counts()
        self.review()
        self.assertEqual(calls, self.counts())

    def test_owner_task_reviewer_can_launch_a_prepared_check(self):
        from pr_review import owner_providers
        from test_owner import REAL_INVOKE
        self.batch['environment_profile'] = {'version': 1, 'projects': {'root': {'directory': '.'}}}
        self.batch['settings']['review']['provider'] = 'codex'
        self.session.save(self.state)
        def command(choice, runtime, role, directory, schema):
            script = """pr-review-check root git ls-files --error-unmatch new.txt
echo '{"type":"turn.completed"}'
echo '{"assessment":"accepted"}' > "$1"
"""
            return ['sh', '-ec', script, 'fixture', str(directory / 'result.json')]
        with patch.object(owner_providers, 'argv', side_effect=command):
            result = REAL_INVOKE(self.session, self.state, self.batch, 'review', self.data)
        self.assertEqual(result['assessment'], 'accepted')
        self.assertEqual(self.batch['attempts'][-1]['environment']['projects']['root']['status'], 'ready')

    def test_apply_to_existing_paused_batch_preserves_implementation(self):
        from pr_review.environment_commands import dispatch
        self.batch['status'] = 'paused'
        self.session.save(self.state)
        profile = {'version': 1, 'projects': {'quality': {'directory': '.'}}}
        request = {'session': str(self.session.path), 'batch': self.batch['id']}
        dispatch(dict(request, command='environment-save', profile=profile))
        before = self.session.load()
        self.assertTrue(dispatch(dict(request, command='environment-apply', preview=True))['changed'])
        self.assertEqual(before, self.session.load())
        dispatch(dict(request, command='environment-apply'))
        updated = self.session.load()['batches'][-1]
        self.assertEqual(updated['checkpoint'], self.batch['checkpoint'])
        self.assertEqual(updated['tasks'], self.batch['tasks'])
        self.assertEqual(updated['environment_profile'], profile)
        self.assertFalse(dispatch(dict(request, command='environment-apply'))['changed'])

    def test_generation_through_cli_and_preview_through_api(self):
        # Exercise the same installed entry points as Emacs, with a scripted Codex.
        from pr_review.common import ROOT
        cli = str(ROOT / 'bin/pr-review')
        profile = {'version': 1, 'projects': {'root': {'directory': '.', 'readiness': [['git', 'status', '--short']]}}}
        with tempfile.TemporaryDirectory() as temp:
            tool = Path(temp) / 'codex'
            invalid = {'version': 1, 'projects': {'root': {'directory': '.', 'environment': {'XDG_CACHE_HOME': '{cache}'}}}}
            tool.write_text('#!' + sys.executable + '\nimport json, sys\nfrom pathlib import Path\n'
                            'assert "local edit" in Path("source.txt").read_text()\n'
                            'out = Path(sys.argv[sys.argv.index("-o") + 1])\n'
                            'prompt = sys.stdin.read()\n'
                            'assert "PYTHONPATH" in prompt and "Tracked lockfile paths" in prompt\n'
                            'correcting = out.parent.name == "correction"\n'
                            'assert not correcting or "root.environment.XDG_CACHE_HOME is reserved" in prompt\n'
                            f'profile = {profile!r} if correcting else {invalid!r}\n'
                            'out.write_text(json.dumps({"profile_json": json.dumps(profile)}))\n')
            tool.chmod(0o700)
            target = ['--session', str(self.session.path), '--batch', self.batch['id']]
            env = dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'])
            generated = subprocess.run([cli,'environment','generate',*target],env=env,capture_output=True,text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            saved = json.loads(environment.location(self.session.repo.project).read_text())
            self.assertTrue((Path(saved['logs']) / 'usage' / 'ledger.json').is_file())
            self.assertIn('environment/generate', json.dumps(saved['usage']))
            shown = json.loads(subprocess.check_output([cli,'environment','show',*target],env=env))
            self.assertEqual(shown['profile'], profile)
            self.batch['status'] = 'paused'
            self.session.save(self.state)
            request = dict(protocol=1,command='environment-apply',session=str(self.session.path),
                           batch=self.batch['id'],preview=True)
            raw = subprocess.check_output([cli,'api'], input=json.dumps(request),text=True,env=env)
            self.assertTrue(json.loads(raw.splitlines()[-1])['result']['changed'])
            tool.write_text('#!/bin/sh\nexit 2\n')
            failed = subprocess.run([cli,'environment','generate',*target],env=env,capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(subprocess.check_output([cli,'environment','show',*target],env=env)), shown)

    def test_setup_mutation_invalidates_candidate_before_provider_runs(self):
        self.single_reviewer()
        self.batch['environment_profile'] = {'version': 1, 'projects': {'root': {
            'directory': '.', 'setup': [[sys.executable, '-c',
                'from pathlib import Path; Path("source.txt").write_text("altered")']]}}}
        self.session.save(self.state)
        with self.assertRaisesRegex(ApiError, 'differs from the candidate'):
            self.review()
        self.assertFalse(self.marks_text())
        self.assertIn('local edit', (self.worktree / 'source.txt').read_text())

    def test_failed_readiness_reaches_synthesis_and_retries_on_resume(self):
        self.single_reviewer()
        self.batch['environment_profile'] = {'version': 1, 'projects': {'root': {
            'directory': '.', 'readiness': [[sys.executable, '-c',
                'from pathlib import Path; assert Path("{cache}/registry-online").is_file(), "registry offline"']]}}}
        self.session.save(self.state)
        self.verdict('incomplete')
        result = self.review()
        self.assertEqual(result.status, 'incomplete')
        self.assertIn('registry offline', self.stdin_copy.read_text())
        profile = self.batch['environment_profile']
        cache = environment.location(self.session.repo.project).parent / 'cache' / environment.identity(profile) / 'root'
        (cache / 'registry-online').touch()
        self.verdict('passed')
        self.assertEqual(self.review().status, 'completed')
        self.assertEqual(self.manifest()['phases']['synthesis']['environment']['projects']['root']['status'], 'ready')
