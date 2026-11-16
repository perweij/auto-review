"""Offline contracts for the opt-in forge smoke runner."""
import json
import io
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
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


class PublicationRecoveryTest(unittest.TestCase):
    def setUp(self):
        from forge_smoke.scenarios import publication_recovery
        self.scenario = publication_recovery
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = SimpleNamespace(directory=Path(self.temp.name),
            root=Path(__file__).resolve().parents[1], env={'PATH': '/native/bin'},
            data={'fixtures': {'github': {'number': 7}}})
        self.original_env = dict(self.run.env)
        self.wrapper = self.run.directory / 'fault-bin/gh'
        self.target = self.run.root / 'tests/forge_smoke/fault.py'
        self.view = dict(generation='generation', revision='revision', comments=[])
        self.interrupt = False
        self.run.api = Mock(side_effect=self.api)
        self.forge = SimpleNamespace(kind='github', cwd=self.run.directory, repo='example/test',
            pages=Mock(return_value=[{'body': 'Smoke lost-response recovery ' + self.run.directory.name}]))
        native = patch('shutil.which', return_value='/unused/native-gh')
        native.start()
        self.addCleanup(native.stop)

    def api(self, request, cwd):
        command = request['command']
        if command == 'discover':
            return {'sessions': [{'session': 'session'}]}
        if command == 'read':
            return self.view
        if command == 'mutate':
            if request['operation'] == 'create':
                self.view['comments'].append(dict(id='comment', status='unstaged',
                    body=request['body'], placement=request['placement']))
            else:
                self.view['comments'][0]['status'] = 'staged'
            return {'review': self.view, 'changed': ['comment']}
        if command == 'prepare':
            return dict(ready=True, review=self.view, confirmation='confirmation')
        if command == 'publish':
            self.assertTrue(self.wrapper.is_symlink())
            self.assertEqual(self.target, self.wrapper.resolve())
            self.assertEqual(str(self.wrapper.parent) + os.pathsep + self.original_env['PATH'], self.run.env['PATH'])
            config = Path(self.run.env['SMOKE_FAULT_FILE'])
            evidence = json.loads(config.read_text())
            self.assertEqual('repos/example/test/pulls/7/reviews', evidence['endpoint'])
            self.assertEqual('after', evidence['mode'])
            if self.interrupt:
                raise KeyboardInterrupt()
            evidence.update(used=True, forwarded_exit=0, response_bytes=10)
            config.write_text(json.dumps(evidence))
            raise Failure('simulated lost response')
        if command == 'resolve':
            self.assertEqual(self.original_env, self.run.env)
            config = self.run.directory / 'fault.json'
            if config.exists() and json.loads(config.read_text()).get('used'):
                self.view['comments'][0].update(status='published', receipt={'id': 42})
            return {'review': self.view}
        self.fail('Unexpected API request: ' + str(request))

    def test_fresh_wrapper_reaches_reconciliation(self):
        self.scenario(self.run, self.forge)
        self.assertEqual(self.target, self.wrapper.readlink())
        self.assertEqual(self.original_env, self.run.env)
        self.assertEqual('resolve', self.run.api.call_args.args[0]['command'])
        self.forge.pages.assert_called_once_with('/pulls/7/reviews')

    def test_interrupted_scenario_reuses_retained_wrapper(self):
        self.run.env['SMOKE_FAULT_FILE'] = '/previous/fault.json'
        self.original_env = dict(self.run.env)
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.scenario(self.run, self.forge)
        self.assertEqual(self.original_env, self.run.env)
        self.assertTrue(self.wrapper.is_symlink())
        self.forge.pages.assert_not_called()
        retained = self.wrapper.lstat()
        self.interrupt = False
        self.scenario(self.run, self.forge)
        self.assertEqual(retained.st_ino, self.wrapper.lstat().st_ino)
        self.assertEqual(self.original_env, self.run.env)
        self.assertEqual('resolve', self.run.api.call_args.args[0]['command'])
        self.forge.pages.assert_called_once_with('/pulls/7/reviews')

    def test_incomplete_used_fault_evidence_never_rearms_publication(self):
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.scenario(self.run, self.forge)
        config = self.run.directory / 'fault.json'
        evidence = json.loads(config.read_text())
        evidence['used'] = True
        config.write_text(json.dumps(evidence))
        retained = config.read_bytes()
        self.run.api.reset_mock()
        with self.assertRaisesRegex(Failure, 'Fault did not hide an accepted remote write'):
            self.scenario(self.run, self.forge)
        self.assertNotIn('publish', [call.args[0]['command'] for call in self.run.api.call_args_list])
        self.assertEqual(retained, config.read_bytes())
        self.assertEqual(self.original_env, self.run.env)

    def test_conflicting_fault_configuration_is_preserved(self):
        config = self.run.directory / 'fault.json'
        config.write_text(json.dumps(dict(method='POST', endpoint='other', mode='after')))
        retained = config.read_bytes()
        with self.assertRaisesRegex(Failure, 'Conflicting publication fault configuration'):
            self.scenario(self.run, self.forge)
        self.assertNotIn('publish', [call.args[0]['command'] for call in self.run.api.call_args_list])
        self.assertEqual(retained, config.read_bytes())
        self.assertEqual(self.original_env, self.run.env)

    def test_accepted_publication_resumes_persisted_state_without_another_post(self):
        self.assert_persisted_retry('accepted')

    def test_reconciliation_interruption_resumes_without_another_post(self):
        self.assert_persisted_retry('resolve')

    def test_remote_verification_failure_resumes_without_another_post(self):
        self.assert_persisted_retry('verification')

    def assert_persisted_retry(self, interruption):
        from test_api import ApiTest
        from pr_review.api import dispatch
        from pr_review import publication
        fixture = ApiTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        github, posts = fixture.fake_github(ambiguous_review=True)
        interrupted = False

        def remote(session, endpoint, payload=None, **options):
            nonlocal interrupted
            if payload is None:
                return github(session, endpoint, **options)
            try:
                return github(session, endpoint, payload, **options)
            finally:
                config = Path(self.run.env['SMOKE_FAULT_FILE'])
                evidence = json.loads(config.read_text())
                evidence.update(used=True, forwarded_exit=0, response_bytes=10)
                config.write_text(json.dumps(evidence))
                if interruption == 'accepted' and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

        def api(request, cwd):
            nonlocal interrupted
            result = dispatch({'protocol': 1, **request})
            if request['command'] == interruption and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        def reviews(endpoint):
            nonlocal interrupted
            if interruption == 'verification' and not interrupted:
                interrupted = True
                raise Failure('Remote verification unavailable')
            return github(fixture.session, 'reviews')

        self.run.api = api
        self.forge.cwd = fixture.repo
        self.forge.pages = reviews
        with patch.object(publication, 'repository_operation'), \
                patch.object(publication, 'gh', side_effect=remote):
            with self.assertRaises(Failure if interruption == 'verification' else KeyboardInterrupt):
                self.scenario(self.run, self.forge)
            self.assertEqual(1, len(posts))
            status = 'ambiguous' if interruption == 'accepted' else 'published'
            action = fixture.session.load()['batches'][0]['actions'][0]
            self.assertEqual(status, action['status'])
            self.assertEqual('unknown' if interruption == 'accepted' else 'confirmed', action['operations'][0]['delivery'])
            self.assertEqual(self.original_env, self.run.env)
            retained = self.wrapper.lstat()
            evidence = (self.run.directory / 'fault.json').read_bytes()
            self.scenario(self.run, self.forge)
            self.assertEqual(1, len(posts))
            self.assertEqual(retained.st_ino, self.wrapper.lstat().st_ino)
            self.assertEqual(evidence, (self.run.directory / 'fault.json').read_bytes())
            self.assertEqual(self.original_env, self.run.env)
            state = fixture.session.load()
            comments = [c for c in state['comments'] if c['body'].startswith('Smoke lost-response recovery ')]
            self.assertEqual(1, len(comments))
            self.assertEqual('published', comments[0]['status'])
            self.assertTrue(comments[0]['receipt'])
            self.assertEqual(1, len(state['batches']))
            # A later retry after reconciliation must also remain a read/recovery operation.
            self.scenario(self.run, self.forge)
            self.assertEqual(1, len(posts))

    def test_conflicting_wrapper_is_preserved_before_publication(self):
        self.wrapper.parent.mkdir()
        other = self.run.directory / 'other'
        other.write_text('unrelated executable')
        for kind in ('file', 'symlink', 'dangling-symlink'):
            with self.subTest(kind=kind):
                self.wrapper.unlink(missing_ok=True)
                if kind == 'file':
                    self.wrapper.write_text('existing entry')
                else:
                    self.wrapper.symlink_to(other if kind == 'symlink' else other.with_name('missing'))
                retained = self.wrapper.lstat()
                self.run.api.reset_mock()
                with self.assertRaisesRegex(Failure, 'Conflicting fault wrapper'):
                    self.scenario(self.run, self.forge)
                self.assertEqual(retained, self.wrapper.lstat())
                self.assertEqual(self.original_env, self.run.env)
                self.assertNotIn('publish', [call.args[0]['command'] for call in self.run.api.call_args_list])
                if kind == 'file':
                    self.assertEqual('existing entry', self.wrapper.read_text())
                else:
                    self.assertEqual(other if kind == 'symlink' else other.with_name('missing'), self.wrapper.readlink())


class IssuePlanRecoveryTest(unittest.TestCase):
    def test_retry_keeps_fault_evidence_and_does_not_repeat_an_accepted_plan(self):
        from forge_smoke.scenarios import issue_plan_recovery
        for mode in ('before', 'after'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                run = SimpleNamespace(directory=Path(directory), root=Path(__file__).resolve().parents[1],
                                      env={'PATH': '/native/bin'}, data={}, save=Mock())
                original_env = dict(run.env)
                forge = SimpleNamespace(repo='example/test', cwd=Path(directory),
                    issue=Mock(return_value={'number': 7}),
                    pages=Mock(return_value=[{'body': '## Outcome\n\n## Verification'}]))
                config = run.directory / ('issue-fault-' + mode + '.json')
                calls, posts = [], []
                def command(args, **kwargs):
                    if '--inspect-writes' in args:
                        return 0, json.dumps({'actions': {'plan:1': {'readback': {
                            'outcome': 'accepted' if posts else 'absent'}}}})
                    if '--retry-absent' in args:
                        posts.append('plan')
                        return 0, '{}'
                    calls.append(args)
                    if len(calls) == 1:
                        evidence = json.loads(config.read_text())
                        evidence['used'] = True
                        if mode == 'after':
                            posts.append('plan')
                            evidence.update(forwarded_exit=0, response_bytes=10)
                        config.write_text(json.dumps(evidence))
                        return 5, '{}'
                    return (3, '{"outcome":"needs_attention"}') if len(calls) == 2 else (2, '{"outcome":"approval"}')
                run.command = command
                with patch('shutil.which', return_value='/native/gh'):
                    with self.assertRaisesRegex(Failure, 'Recovered plan did not reach'):
                        issue_plan_recovery(run, forge, mode)
                    retained = config.read_bytes()
                    issue_plan_recovery(run, forge, mode)
                    self.assertEqual(3, len(calls))
                    self.assertEqual(['plan'], posts)
                    self.assertEqual(retained, config.read_bytes())
                    self.assertEqual(original_env, run.env)


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
