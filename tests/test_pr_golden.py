"""PR review prompts, argv and cache identities must not change when the engine is shared.

The expected values in ``fixtures/pr-golden.json`` were captured from the
pre-extraction implementation. Comparison fingerprints hash the captured inputs
after installation/run paths are normalized; production cache identity is not
changed. Regenerate them only for a deliberate PR-review
change: ``UPDATE_PR_GOLDEN=1 python3 tests/test_pr_golden.py``.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import cache, pipeline, providers, settings
from pr_review.common import ROOT
from pr_review.reviews import Review
from pr_review.storage import hash_file

GOLDEN = Path(__file__).with_name('fixtures') / 'pr-golden.json'
SLOT = lambda number, provider, model, effort: {'id': f'reviewer-{number}', 'provider': provider, 'model': model, 'effort': effort}
CASES = {
    'one-slot-matching-synthesis': dict(
        reviewer_slots=[SLOT(1, 'claude', 'opus', 'high')],
        processing={'provider': 'claude', 'model': 'opus', 'effort': 'high'}),
    'one-slot-differing-synthesis': dict(
        reviewer_slots=[SLOT(1, 'claude', 'opus', 'high')],
        processing={'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}),
    'two-slots-mixed-providers': dict(
        reviewer_slots=[SLOT(1, 'claude', 'opus', 'high'), SLOT(2, 'codex', 'gpt-6.1-sol', 'high')],
        processing={'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}, synthesis_tests='full'),
    'two-slots-same-claude-provider': dict(
        reviewer_slots=[SLOT(1, 'claude', 'opus', 'high'), SLOT(2, 'claude', 'haiku', 'low')],
        processing={'provider': 'claude', 'model': 'sonnet', 'effort': 'medium'}),
    'two-slots-same-codex-provider': dict(
        reviewer_slots=[SLOT(1, 'codex', 'gpt-6.1-sol', 'high'), SLOT(2, 'codex', 'gpt-6.1-terra', 'low')],
        processing={'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}),
}
DATES = {'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z',
         'GIT_AUTHOR_NAME': 'Golden', 'GIT_AUTHOR_EMAIL': 'golden@example.com',
         'GIT_COMMITTER_NAME': 'Golden', 'GIT_COMMITTER_EMAIL': 'golden@example.com'}


def normalize(value, directory):
    text = json.dumps(value, sort_keys=True)
    return json.loads(text.replace(str(directory), '<run>').replace(str(ROOT), '<root>'))


def build(configuration, directory):
    path = Path(directory) / 'run'
    for name in ('reviews', 'prompts', 'logs', 'context', 'discussion'):
        (path / name).mkdir(parents=True, exist_ok=True)
    for name in ('pr.json', 'pr-discussion.json', 'pr-reviews.json', 'pr-inline-comments.json',
                 'ci-actions-runs.json', 'ci-statuses.json', 'pr.diff', 'diffstat.txt'):
        (path / 'context' / name).write_text(f'fixed {name}\n')
    run = Review.__new__(Review)
    run.path = run.root = path
    run.repo = SimpleNamespace(base_ref='refs/pr-review/7/base', head_ref='refs/pr-review/7/head',
                               identity='github.com/org/repo', pr=7)
    run.settings = settings.validate(configuration)
    run.runtime = providers.Runtime()
    run.tools = {'claude': 'claude 1.0', 'codex': 'codex 1.0'}
    run.manifest = {'snapshot': {'head': 'a' * 40, 'base': 'b' * 40, 'merge_base': 'c' * 40}, 'phases': {},
                    'warnings': ['example warning']}
    run.save = Mock()
    return run


def capture(name, configuration):
    with tempfile.TemporaryDirectory() as directory:
        run = build(configuration, directory)
        slots = run.settings['reviewer_slots']
        result = {'dual': run.dual, 'settings': run.settings}
        # Prompt a single-reviewer synthesis before any slot report exists.
        pipeline.render_synthesis(run)
        result['prompt-without-reports'] = (path := run.path / 'prompts/synthesis-prompt.txt').read_text()
        if not run.dual:
            result['synthesis-inputs-without-reports'] = cache.inputs(run, 'synthesis')
        independent = {}
        for slot in slots:
            stage = slot['id']
            inputs = cache.inputs(run, stage)
            report = run.path / 'reviews' / (stage + '.md')
            report.write_text(f'Report from {stage}\n')
            run.manifest['phases'][stage] = dict(status='completed', output_hash=hash_file(report), exit_status=0,
                                                 inputs=inputs, fingerprint=cache.fingerprint(inputs))
            independent[stage] = dict(argv=run.command(stage), inputs=inputs, fingerprint=cache.fingerprint(inputs))
        result['slots'] = independent
        pipeline.render_synthesis(run)
        result['prompt-with-reports'] = path.read_text()
        inputs = cache.inputs(run, 'synthesis')
        result['synthesis'] = dict(argv=run.command('synthesis'), inputs=inputs, fingerprint=cache.fingerprint(inputs))
        result['contributions-argv'] = run.command('contributions')
        result = normalize(result, run.path)
        # Codex's schema argument contains the installation root. Normalize it
        # before hashing for comparison across checkouts, not after: replacing
        # paths in the displayed inputs cannot normalize an existing hash.
        for stage in [*result['slots'].values(), result['synthesis']]:
            stage['fingerprint'] = cache.fingerprint(stage['inputs'])
        return result


class PrGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_prompts_argv_and_cache_identities_match_pre_extraction_capture(self):
        actual = {name: capture(name, configuration) for name, configuration in CASES.items()}
        if os.environ.get('UPDATE_PR_GOLDEN') == '1':
            GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + '\n')
        expected = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(expected), sorted(actual))
        for name in expected:
            with self.subTest(name):
                self.assertEqual(expected[name], actual[name])

    def test_the_pr_direct_path_uses_its_own_worktree_and_no_disposable_checkout(self):
        from pr_review import isolation, reviews
        with tempfile.TemporaryDirectory() as directory:
            run = build(CASES['one-slot-matching-synthesis'], directory)
            run.worktree = Path(directory) / 'pr-worktree'
            run.repo.verify = Mock()
            run.repo.git = Mock(return_value=SimpleNamespace(stdout=''))
            run.repo.common = Path(directory) / 'common'
            adapter = reviews.PR_ADAPTER
            self.assertEqual(run.worktree, adapter.stage_cwd(run, 'synthesis', None))
            (run.path / 'logs').mkdir(exist_ok=True)
            identity = SimpleNamespace(returncode=0, stdout='\n'.join([str(run.worktree), str(run.repo.common), 'a' * 40]), stderr='')
            with unittest.mock.patch.object(isolation, 'prepare_checkout', side_effect=AssertionError('PR direct path')), \
                    unittest.mock.patch('pr_review.reviews.process', return_value=identity):
                adapter.begin_stage(run, 'synthesis', None, 'claude', {})
            run.repo.verify.assert_called_once_with(run.manifest, run.path)
            self.assertEqual({}, adapter.identity(run, 'synthesis'))     # PR fingerprints gain nothing

    def test_each_case_is_distinguished_by_its_own_fingerprints(self):
        golden = json.loads(GOLDEN.read_text())
        fingerprints = [value['synthesis']['fingerprint'] for value in golden.values()]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))


if __name__ == '__main__':
    unittest.main()
