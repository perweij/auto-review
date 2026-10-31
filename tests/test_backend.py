"""Migration evidence: original Bash bytes, lifecycle failures, and boundaries."""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import cache, discussion, providers, validation
from pr_review.common import ApiError, read_json
from pr_review.processes import run, cancellation_scope
from pr_review.storage import hash_file, json_bytes

ROOT = Path(__file__).resolve().parents[1]


class LegacyBytesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.fixture = json.loads((ROOT / 'tests/fixtures/bash-v2-session.json').read_text())
        for name, text in self.fixture['files'].items():
            file = self.path / name
            file.parent.mkdir(exist_ok=True, parents=True)
            file.write_text(text.replace(self.fixture['directory'], str(self.path)))
        manifest = read_json(self.path / 'manifest.json')
        original = json.loads(self.fixture['files']['manifest.json'])
        for stage, record in manifest['phases'].items():
            if 'stdout_log' in record:
                record['stdout_log'] = original['phases'][stage]['stdout_log']
        self.run = SimpleNamespace(path=self.path, manifest=manifest, original_manifest=copy.deepcopy(manifest),
            settings=manifest['configuration'], tools=manifest['tools'], dual=True, save=lambda: None,
            original_prompts={stage: (self.path / f'prompts/{stage}-prompt.txt').read_bytes() for stage in ('synthesis', 'contributions')})
        # Byte-compatibility tests replay the frozen invocation, not today's
        # instructions. New review prompts must invalidate those cached results.
        commands = {c['stage']: c['argv'] for c in read_json(self.path / 'logs/commands.json')}
        self.run.command = lambda stage: copy.deepcopy(commands[stage])

    def test_current_review_instructions_invalidate_legacy_reviewer_results(self):
        self.run.command = lambda stage: providers.review_command(self.run.settings, providers.Runtime(), stage, self.path, 'refs/pr-review/7/base')
        for stage, name in [('claude', 'claude-independent.md'), ('codex', 'codex.md'), ('synthesis', 'synthesis.json')]:
            with self.subTest(stage=stage):
                self.assertFalse(cache.reusable(self.run, stage, self.path / 'reviews' / name, cache.inputs(self.run, stage)))

    def test_saved_sandbox_failure_report_is_not_reused(self):
        for stage, name in [('codex', 'codex.md'), ('synthesis', 'synthesis.json')]:
            with self.subTest(stage=stage):
                output = self.path / 'reviews' / name
                report = ("Review blocked: commands failed before execution with "
                          "bwrap: Can't mount proc on /proc: Operation not permitted")
                output.write_text(json.dumps({'report_md': report}) if stage == 'synthesis' else report)
                self.run.manifest['phases'][stage]['output_hash'] = hash_file(output)
                self.assertFalse(cache.reusable(self.run, stage, output, cache.inputs(self.run, stage)))

    def test_original_cache_bytes_and_validated_reuse_for_every_stage(self):
        for stage, name in [('claude','claude-independent.md'), ('codex','codex.md'), ('synthesis','synthesis.json'), ('contributions','contributions.json')]:
            with self.subTest(stage=stage):
                record = self.run.manifest['phases'][stage]
                self.assertEqual(cache.fingerprint(record['inputs']), record['fingerprint'])
                self.assertTrue(cache.reusable(self.run, stage, self.path / 'reviews' / name, cache.inputs(self.run, stage)))
        self.assertEqual(cache.context_hash(self.path / 'context'), self.run.manifest['phases']['context']['output_hash'])

    def test_pre_input_cache_adoption_checks_saved_commands_prompts_and_outputs(self):
        for record in self.run.manifest['phases'].values():
            record.pop('inputs', None)
            record['fingerprint'] = 'legacy-installation-hash'
        for stage, name in [('claude','claude-independent.md'), ('codex','codex.md'), ('synthesis','synthesis.json'), ('contributions','contributions.json')]:
            with self.subTest(stage=stage):
                self.assertTrue(cache.reusable(self.run, stage, self.path / 'reviews' / name, cache.inputs(self.run, stage)))
        path = self.path / 'reviews/codex.md'
        path.write_text('Modified saved output')
        self.assertFalse(cache.reusable(self.run, 'codex', path, cache.inputs(self.run, 'codex')))

    def test_unknown_validator_hash_is_not_assumed_compatible(self):
        record = self.run.manifest['phases']['synthesis']
        record['inputs']['artifacts']['validator'] = 'unknown'
        record['fingerprint'] = cache.fingerprint(record['inputs'])
        self.assertFalse(cache.reusable(self.run, 'synthesis', self.path / 'reviews/synthesis.json', cache.inputs(self.run, 'synthesis')))

    def test_normalized_discussion_matches_original_bytes_and_hash(self):
        folder = Path(self.run.manifest['discussion']['path']).parent
        value = discussion.normalize(*(read_json(folder / (n + '.json')) for n in ('issues', 'reviews', 'inline', 'threads')))
        self.assertEqual(json_bytes(value), (folder / 'discussion.json').read_bytes())
        self.assertEqual(json_bytes(discussion.external(value, []), sort_keys=True), (folder / 'external.json').read_bytes())
        self.assertEqual(hash_file(folder / 'external.json'), self.run.manifest['discussion']['hash'])

    def test_model_validators_accept_original_results_and_reject_missing_coverage(self):
        findings = validation.synthesis(read_json(self.path / 'reviews/synthesis.json'), new=True)
        conversation = read_json(self.run.manifest['discussion']['path'])
        value = validation.contributions(read_json(self.path / 'reviews/contributions.json'), findings, conversation)
        value['contributions'] = []
        with self.assertRaises(ApiError):
            validation.contributions(value, findings, conversation)


class SupervisorTest(unittest.TestCase):
    def test_repository_commands_survive_more_than_ninety_seconds(self):
        from pr_review.common import command
        clock = time.monotonic
        calls = 0
        def elapsed():
            nonlocal calls
            calls += 1
            return clock() + (120 if calls > 1 else 0)
        with patch('pr_review.processes.time.monotonic', side_effect=elapsed):
            result = command([sys.executable, '-c', "import time; time.sleep(.05); print('finished')"])
        self.assertEqual(result.stdout, 'finished\n')

    def test_explicit_command_deadline_still_applies(self):
        from pr_review.common import command
        with self.assertRaises(ApiError) as caught:
            command([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=.05)
        self.assertEqual(caught.exception.details['exit_code'], 124)

    def test_partial_json_and_final_unterminated_record(self):
        code = "import sys,time; sys.stdout.write('{\\\"type\\\":'); sys.stdout.flush(); time.sleep(.05); sys.stdout.write('\\\"turn.completed\\\"}')"
        result = run([sys.executable, '-c', code], model_events=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.events, [{'type': 'turn.completed'}])

    def test_retryable_error_does_not_hide_a_successful_exit(self):
        code = "print('malformed json'); print('{\"type\":\"item.completed\",\"item\":null}'); print('{\"type\":\"error\",\"message\":\"retry\"}'); print('{\"type\":\"turn.completed\"}')"
        self.assertEqual(run([sys.executable, '-c', code], model_events=True).returncode, 0)

    def test_error_event_cannot_succeed_with_a_zero_process_exit(self):
        result = run([sys.executable, '-c', "print('{\"type\":\"turn.failed\"}')"], model_events=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.reason, 'codex_turn_failed')

    def test_descendant_holding_pipes_after_leader_exit_is_bounded(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); print('saved',flush=True)"
        before = time.monotonic()
        result = run([sys.executable, '-c', code], timeout=None)
        self.assertLess(time.monotonic() - before, 9)
        self.assertEqual(result.stdout.strip(), 'saved')

    def test_timeout_is_distinct_and_preserves_raw_output(self):
        result = run([sys.executable, '-c', "import time; print('checkpoint',flush=True); time.sleep(60)"], timeout=.1)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.reason, 'timeout')
        self.assertIn('checkpoint', result.stdout)

    def test_cancel_before_spawn_cannot_launch_a_process(self):
        with self.assertRaises(ApiError) as raised:
            with cancellation_scope() as token, patch('pr_review.processes.subprocess.Popen') as spawn:
                token.request(signal.SIGTERM)
                run(['never-executed'])
        spawn.assert_not_called()
        self.assertEqual(raised.exception.code, 'cancelled')

    def test_broken_response_callback_reaps_owned_child(self):
        pids = []
        def disconnected(_stream, line):
            pids.append(int(line))
            raise BrokenPipeError('Frontend disconnected')
        with self.assertRaises(ApiError):
            run([sys.executable, '-c', 'import os,time; print(os.getpid(),flush=True); time.sleep(60)'],
                on_line=disconnected, timeout=5)
        self.assertEqual(len(pids), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_large_stdin_and_output_do_not_deadlock(self):
        code = "import sys; sys.stdout.buffer.write(b'x'*300000); sys.stdout.flush(); data=sys.stdin.buffer.read(); print(len(data))"
        result = run([sys.executable, '-c', code], input_bytes=b'y'*400000, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith('400000\n'))

    def test_large_model_result_keeps_success_and_error_semantics(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as name:
                output = Path(name) / 'events.jsonl'
                code = ("import json; print(json.dumps({'type':'result','is_error':" + repr(failed) +
                        ",'result':'x'*(5*1024*1024)}))")
                result = run([sys.executable, '-c', code], model_events=True,
                             stdout_path=output, timeout=5)
                self.assertEqual(len(result.events[-1]['result']), 5*1024*1024)
                self.assertEqual(result.events[-1], json.loads(output.read_bytes()))
                self.assertEqual(result.reason, 'model_result_error' if failed else '')
                self.assertEqual(result.returncode != 0, failed)

    def test_long_stderr_is_logged_without_model_record_parsing(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'stderr.log'
            code = "import sys; sys.stderr.write('x'*(5*1024*1024)); print('{\"type\":\"turn.completed\"}')"
            with patch.dict(os.environ, {'PR_REVIEW_MAX_EVENT_BYTES':'1024'}):
                result = run([sys.executable, '-c', code], model_events=True,
                             stderr_path=output, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(output.stat().st_size, 5*1024*1024)

    def test_configured_event_limit_applies_to_complete_and_partial_records(self):
        for newline in ('', '\n'):
            with self.subTest(newline=newline), patch.dict(os.environ, {'PR_REVIEW_MAX_EVENT_BYTES':'1024'}):
                code = 'import sys; sys.stdout.write("x"*2048+' + repr(newline) + ')'
                with self.assertRaises(ApiError) as caught:
                    run([sys.executable, '-c', code], model_events=True, timeout=5)
                self.assertEqual(caught.exception.code, 'output_limit')

    def test_environment_is_filtered_as_a_unit_without_mutating_parent(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'key','GIT_CONFIG_VALUE_0':'secret','GH_TOKEN':'secret','OPENAI_API_KEY':'own','ANTHROPIC_API_KEY':'other'}):
            env, removed = providers.environment('codex')
            self.assertEqual(env['OPENAI_API_KEY'], 'own')
            for key in ('GIT_CONFIG_COUNT','GIT_CONFIG_KEY_0','GIT_CONFIG_VALUE_0','GH_TOKEN','ANTHROPIC_API_KEY'):
                self.assertNotIn(key, env)
                self.assertIn(key, removed)
            self.assertIn('GH_TOKEN', os.environ)

class ProviderPolicyTest(unittest.TestCase):
    def test_all_codex_model_commands_use_container_isolation(self):
        from pr_review.settings import DEFAULTS
        settings = {**DEFAULTS, 'processing_provider': 'codex'}
        runtime, directory = providers.Runtime(), Path('/tmp/artifacts')
        commands = [providers.review_command(settings, runtime, stage, directory, 'base')
                    for stage in ('codex', 'synthesis', 'contributions', 'followup',
                                  'suggestions', 'suggestion-verify')]
        commands += [providers.probe_command('codex', runtime, directory / 'probe.txt'),
                     providers.adaptation_command(settings)]
        for args in commands:
            with self.subTest(args=args[:3]):
                modes = [a for a in args if a.startswith('sandbox_mode=')]
                self.assertEqual(modes, ['sandbox_mode=danger-full-access'])
                self.assertIn('approval_policy=never', args)
                self.assertFalse(any(a.startswith('sandbox_workspace_write.') for a in args))

    def test_reported_sandbox_failure_diagnostic(self):
        error = "bwrap: Can't mount proc on /proc: Operation not permitted"
        self.assertTrue(providers.reported_execution_failure('Review blocked: ' + error))
        self.assertTrue(providers.reported_execution_failure(json.dumps({'report_md': 'Assessment incomplete. ' + error})))
        self.assertFalse(providers.reported_execution_failure('The regression test covers ' + error))
        self.assertFalse(providers.reported_execution_failure('Review blocked by missing dependencies'))

    def test_tool_text_is_not_a_terminal_error(self):
        from pr_review.processes import event_error
        self.assertEqual(event_error({'type':'item.completed','item':{'text':'not a git repository'}}), '')
        self.assertEqual(event_error({'type':'error','message':'not a git repository'}), 'git_repository_unavailable')

    def test_buffered_and_streamed_commands_have_same_identity_without_hiding_model_changes(self):
        prior = ['claude','-p','--model','saved-model','--output-format','json','--json-schema','{}']
        current = ['claude','-p','--model','saved-model','--output-format','stream-json','--verbose','--include-partial-messages','--json-schema','{}']
        self.assertEqual(providers.cache_command(prior), providers.cache_command(current))
        current[3] = 'other-model'
        self.assertNotEqual(providers.cache_command(prior), providers.cache_command(current))

    def test_git_preflight_fails_before_a_model_and_saves_diagnostics(self):
        from test_api import ApiTest
        from pr_review.reviews import Review
        from pr_review.repository import Repository
        from pr_review.settings import DEFAULTS
        fixture = ApiTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        run_state = Review.__new__(Review)
        run_state.path, run_state.worktree = fixture.path, fixture.repo
        run_state.manifest = copy.deepcopy(fixture.manifest)
        run_state.manifest['phases'] = {}
        run_state.settings, run_state.runtime = DEFAULTS, providers.Runtime()
        run_state.repo = Repository(fixture.repo, Path(fixture.manifest['git_common_dir']), 'github.com/org/repo', 7, 'origin')
        (fixture.path / 'logs').mkdir(exist_ok=True)
        env = {**os.environ, 'GIT_CONFIG_COUNT':'1','GIT_CONFIG_VALUE_0':'true'}
        env.pop('GIT_CONFIG_KEY_0', None)
        with patch.object(run_state.repo, 'verify'), patch('pr_review.cache.inputs', return_value={}), patch('pr_review.providers.environment', return_value=(env, [])):
            with self.assertRaisesRegex(ApiError, "Git cannot read the worktree"):
                run_state.model('codex', ['must-not-be-launched'])
        self.assertTrue((fixture.path / 'logs/codex.git-preflight.log.stderr').stat().st_size)

class BoundaryTest(unittest.TestCase):
    def test_installed_entry_and_symlink_answer_protocol_without_shell_or_model_tools(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / 'installation with spaces'
            for name in ('bin', 'lib', 'prompts'):
                shutil.copytree(ROOT / name, install / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root / 'path'
            path.mkdir()
            (path / 'python3').symlink_to(sys.executable)
            (path / 'pr-review').symlink_to(install / 'bin/pr-review')
            result = subprocess.run([str(path / 'pr-review'), 'api'], cwd=root,
                env={**os.environ, 'PATH':str(path)}, input=json.dumps({'protocol':1, 'command':'capabilities'})+'\n',
                text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('run', json.loads(result.stdout)['result']['commands'])
            help_result = subprocess.run([str(path / 'pr-review'), '--help'], cwd=root,
                env={**os.environ, 'PATH':str(path)}, text=True, capture_output=True, timeout=5)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn('--resume', help_result.stdout)

    def test_cancel_refuses_unverified_or_reused_pid(self):
        from pr_review import operations
        from pr_review.common import atomic_json
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME':directory}):
            identity = 'a'*32
            path = Path(directory) / 'pr-review/operations' / (identity+'.json')
            for saved, current in [(None, None), ('old', 'new')]:
                atomic_json(path, {'pid':1234, 'status':'running', 'start':saved})
                with patch.object(operations, 'process_start', return_value=current), patch.object(operations.os, 'kill') as kill:
                    self.assertFalse(operations.cancel({'operation_id':identity})['cancelled'])
                    kill.assert_not_called()

    def test_legacy_import_is_local_and_keeps_exact_combined_document(self):
        from test_api import ApiTest
        from pr_review.state import Session
        from pr_review.common import atomic_json
        fixture = ApiTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        body = 'Exact legacy text α\r\n\r\n'
        path = fixture.path / 'publication/review-body.md'
        path.write_bytes(body.encode())
        legacy = dict(id='a1', kind='review', status='submitting', marker='legacy marker', viewer='reviewer',
                      event='COMMENT', finding_ids=['F1'], proposal_ids=['p1'], body_md=body, target=None)
        atomic_json(fixture.path / 'publication/actions.json', [legacy])
        before = {p: p.read_bytes() for p in fixture.path.rglob('*') if p.is_file()}
        with patch('pr_review.common.command', side_effect=AssertionError('Local import cannot execute a command')):
            state = Session({'session':str(fixture.path)}).load()
        self.assertEqual(state['documents'][0]['text'], body)
        self.assertEqual(state['batches'][0]['actions'][0]['payload']['body'], body)
        self.assertEqual(state['batches'][0]['actions'][0]['marker'], 'legacy marker')
        self.assertEqual({p: p.read_bytes() for p in fixture.path.rglob('*') if p.is_file()}, before)

    def test_editor_failure_retains_file_and_exact_text(self):
        from pr_review.terminal import edit_text
        import subprocess
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME':directory, 'EDITOR':'editor', 'VISUAL':''}):
            def edit(argv, **_kwargs):
                Path(argv[-1]).write_bytes('Human β\r\n\n'.encode())
                raise subprocess.CalledProcessError(1, argv)
            with patch('pr_review.terminal.subprocess.run', side_effect=edit), self.assertRaises(subprocess.CalledProcessError):
                edit_text('original')
            files = list(Path(directory).rglob('draft-*.md'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), 'Human β\r\n\n'.encode())
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
