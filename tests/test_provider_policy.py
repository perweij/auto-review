"""Every model entry point must retain the outer-container execution policy."""
import json
from pathlib import Path
import tempfile
import unittest

from pr_review import issue_harness, models, owner_providers, providers, settings


class ProviderPolicyTest(unittest.TestCase):
    def assert_policy(self, args, provider):
        self.assertNotIn('bwrap', args)
        if provider == 'claude':
            value = json.loads(args[args.index('--settings') + 1])
            self.assertIs(False, value['sandbox']['enabled'])
        else:
            values = [args[i + 1] for i, arg in enumerate(args) if arg == '-c']
            self.assertIn('sandbox_mode=danger-full-access', values)
            self.assertIn('approval_policy=never', values)

    def test_all_execution_paths_including_resumed_implementation(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            schema = directory / 'schema.json'
            schema.write_text('{"type":"object"}')
            for provider in ('claude', 'codex'):
                choice = dict(provider=provider, model='test-model', effort='high')
                config = settings.validate({
                    'reviewer_slots': [dict(choice, id='reviewer-1'), dict(choice, id='reviewer-2')],
                    'processing': choice})
                runtime = providers.Runtime()
                commands = {stage: providers.review_command(config, runtime, stage, directory, 'refs/base')
                            for stage in ('reviewer-1', 'reviewer-2', 'synthesis', 'contributions',
                                          'suggestions', 'suggestion-verify', 'followup')}
                commands.update({role: owner_providers.argv(choice, runtime, role, directory, schema)
                                 for role in ('fix', 'review', 'plan')})
                commands['probe'] = providers.probe_command(provider, runtime, directory / 'response')
                commands['adaptation'] = providers.adaptation_command(config)
                commands['fresh implementation'] = issue_harness.arguments(
                    choice, directory, schema, {'session_id': 'test-session'} if provider == 'claude' else {})
                commands['resumed implementation'] = issue_harness.arguments(
                    choice, directory, schema, {'session_id': 'test-session', 'started': True})
                for label, args in commands.items():
                    with self.subTest(provider=provider, path=label):
                        self.assert_policy(args, provider)
                self.assertNotIn('--resume' if provider == 'claude' else 'resume',
                                 commands['fresh implementation'])
                for slot in ('reviewer-1', 'reviewer-2'):
                    self.assertIn('--no-session-persistence' if provider == 'claude' else '--ephemeral', commands[slot])
                self.assertIn('--resume' if provider == 'claude' else 'resume',
                              commands['resumed implementation'])
                if provider == 'claude':
                    for label in ('reviewer-1', 'synthesis', 'plan', 'review', 'fix'):
                        self.assertIn('--disallowed-tools', commands[label])
                        self.assertIn('Bash(git push:*)', commands[label])
                    self.assertIn('Edit', commands['review'])
                    self.assertNotIn('Edit', commands['fix'])
                    self.assertNotIn('--dangerously-skip-permissions', commands['fix'])

    def test_metadata_discovery_preserves_no_execution_and_hook_settings(self):
        args = models.discovery_command('claude')
        self.assert_policy(args, 'claude')
        self.assertTrue(json.loads(args[args.index('--settings') + 1])['disableAllHooks'])
        self.assertEqual('', args[args.index('--tools') + 1])
        self.assertEqual('dontAsk', args[args.index('--permission-mode') + 1])
        self.assertEqual(['codex', 'app-server', '--listen', 'stdio://'], models.discovery_command('codex'))


if __name__ == '__main__':
    unittest.main()
