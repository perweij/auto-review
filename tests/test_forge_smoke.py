"""Offline contracts for the opt-in forge smoke runner."""
import json
import io
from unittest.mock import patch
from pathlib import Path
import subprocess
import tempfile
import unittest
from forge_smoke.runner import Run, Failure, snapshot


class RunTest(unittest.TestCase):
    def setUp(self):
        output = patch('sys.stdout', new_callable=io.StringIO)
        output.start()
        self.addCleanup(output.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)

    def test_snapshot_includes_untracked_and_does_not_change_index(self):
        first = snapshot(self.root)
        (self.root / 'new.py').write_text('one')
        second = snapshot(self.root)
        self.assertNotEqual(first, second)
        self.assertFalse((self.root / '.git/index').exists())
        (self.root / 'new.py').write_text('two')
        self.assertNotEqual(second, snapshot(self.root))

    def test_resume_rejects_changed_source(self):
        run = Run(self.root, Path(self.temp.name) / 'run')
        (self.root / 'new.py').write_text('new')
        with self.assertRaises(Failure):
            Run(self.root, run.directory, resume=True)

    def test_failed_scenario_is_reported_and_rerunnable(self):
        run = Run(self.root, Path(self.temp.name) / 'run')
        def fail():
            raise Failure('expected failure')
        self.assertFalse(run.scenario('failure', fail))
        self.assertTrue(run.scenario('failure', lambda: None))
        run.report()
        data = json.loads((run.directory / 'report.json').read_text())
        self.assertEqual(['failed', 'passed'], [r['status'] for r in data['results']])

    def test_process_nonzero_does_not_succeed(self):
        run = Run(self.root, Path(self.temp.name) / 'run')
        with self.assertRaises(Failure):
            run.command(['python3', '-c', 'raise SystemExit(7)'])
        self.assertEqual(7, run.data['commands'][0]['exit_code'])


class ProviderContractTest(unittest.TestCase):
    def invoke(self, schema, root):
        import os
        executable = root / 'codex'
        executable.symlink_to(Path(__file__).resolve().parent / 'forge_smoke/provider.py')
        schema_path, output = root / 'schema.json', root / 'result.json'
        schema_path.write_text(json.dumps(schema))
        subprocess.run([str(executable), '--output-schema', str(schema_path), '-o', str(output)],
                       cwd=root, env=dict(os.environ, FIXTURE=str(root)), input='', text=True,
                       capture_output=True, check=True)
        return json.loads(output.read_text())

    def test_planning_output_passes_production_validation(self):
        from pr_review.issue_planning import SCHEMA
        from pr_review.planning_review import validate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'README.md').write_text('Fixture repository')
            value = self.invoke(SCHEMA, root)
            self.assertEqual('plan', validate('draft', value, root)['decision'])

    def test_implementation_really_changes_source_and_passes_validation(self):
        from pr_review.issue_harness import EVIDENCE_SCHEMA, validate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = self.invoke(EVIDENCE_SCHEMA, root)
            validate(value)
            self.assertEqual('ready', value['status'])
            subprocess.run(['python3', '-m', 'unittest', 'test_smoke_total'], cwd=root,
                           capture_output=True, check=True)


class FaultContractTest(unittest.TestCase):
    def test_fault_modes_are_once_only_and_record_accepted_writes(self):
        import os
        for mode in ('before', 'after', 'empty'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                side_effect = root / 'writes'
                executable = root / 'native'
                executable.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\np=Path(' + repr(str(side_effect)) + ')\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint(\'{"id":42}\')\n')
                executable.chmod(0o700)
                config = root / 'fault.json'
                config.write_text(json.dumps(dict(executable=str(executable), endpoint='repos/test/comments', method='POST', mode=mode)))
                command = ['python3', str(Path(__file__).resolve().parent / 'forge_smoke/fault.py'), 'api', '-X', 'POST', 'repos/test/comments']
                env = dict(os.environ, SMOKE_FAULT_FILE=str(config))
                first = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(0 if mode == 'empty' else 1, first.returncode)
                self.assertEqual('', first.stdout)
                evidence = json.loads(config.read_text())
                self.assertTrue(evidence['used'])
                if mode == 'before':
                    self.assertFalse(side_effect.exists())
                else:
                    self.assertEqual(0, evidence['forwarded_exit'])
                    self.assertGreater(evidence['response_bytes'], 0)
                second = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(0, second.returncode)
                self.assertEqual({'id': 42}, json.loads(second.stdout))
                self.assertEqual('x' if mode == 'before' else 'xx', side_effect.read_text())


class CleanupContractTest(unittest.TestCase):
    def test_changed_branch_is_never_deleted(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from forge_smoke.forge import Forge
        forge = Forge.__new__(Forge)
        forge.kind, forge.remote = 'github', 'https://github.com/example/test.git'
        forge.run = SimpleNamespace(directory=Path('/tmp/run-one'), save=Mock(), command=Mock(return_value=(0, 'new refs/heads/fixture\n')),
            data={'objects': [dict(kind='github', type='branch', name='auto-review-smoke/run-one/base', sha='old')]})
        forge.api = Mock()
        with self.assertRaisesRegex(Failure, 'branch changed'):
            forge.cleanup()
        forge.api.assert_not_called()


class TraceContractTest(unittest.TestCase):
    def test_graphql_reads_are_not_misreported_as_mutations(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / 'native'
            native.write_text('#!/usr/bin/env python3\nprint("{}")\n')
            native.chmod(0o700)
            wrapper = root / 'gh'
            wrapper.symlink_to(Path(__file__).resolve().parent / 'forge_smoke/trace.py')
            config, log = root / 'config.json', root / 'trace.jsonl'
            config.write_text(json.dumps(dict(gh=str(native), log=str(log))))
            env = dict(os.environ, SMOKE_TRACE_CONFIG=str(config), SMOKE_ACTOR='application')
            for query in ('query { viewer { login } }', 'mutation { operation }'):
                subprocess.run([str(wrapper), 'api', 'graphql', '--input', '-'],
                    input=json.dumps({'query': query, 'variables': {'body': 'private fixture text'}}),
                    text=True, capture_output=True, check=True, env=env)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(['read', 'mutation'], [r['kind'] for r in records])
            self.assertNotIn('private fixture text', log.read_text())
