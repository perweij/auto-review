"""Independent slot isolation and recovery against disposable Git fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pr_review import isolation, models, pipeline, providers, settings
from pr_review.reviews import Review
from pr_review.sessions import Store


class IsolationTest(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd,
                              check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def test_reviewer_cannot_read_reports_or_previous_private_files(self):
        if not shutil.which('bwrap'):
            self.skipTest('bubblewrap unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state, scratch = root / 'repo', root / 'state', root / 'scratch'
            for path in (repo, state, scratch):
                path.mkdir()
            self.git(repo, 'init', '-q', '-b', 'main')
            self.git(repo, 'config', 'user.name', 'Test')
            self.git(repo, 'config', 'user.email', 'test@example.com')
            (repo / 'source').write_text('snapshot')
            self.git(repo, 'add', 'source')
            self.git(repo, 'commit', '-qm', 'snapshot')
            base = self.git(repo, 'rev-parse', 'HEAD')
            (repo / 'source').write_text('snapshot at PR head')
            self.git(repo, 'commit', '-qam', 'PR head')
            head = self.git(repo, 'rev-parse', 'HEAD')
            self.git(repo, 'update-ref', 'refs/review/head', head)
            self.git(repo, 'update-ref', 'refs/review/base', base)
            self.git(repo, 'checkout', '-q', '--detach', base)
            self.git(repo, 'branch', '-f', 'main', base)
            (state / 'other-review.md').write_text('private finding')
            run = SimpleNamespace(scratch=scratch, worktree=repo, root=state, path=state,
                repo=SimpleNamespace(common=repo / '.git', head_ref='refs/review/head', base_ref='refs/review/base'),
                manifest={'snapshot': {'head': head, 'base': base}})
            with patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'xdg'), 'HOME': str(root / 'home'),
                                         'CODEX_HOME': str(root / 'home/.codex'),
                                         'CLAUDE_CONFIG_DIR': str(root / 'home/.claude'),
                                         'GIT_DIR': str(root / 'unrelated-git')}):
                (root / 'xdg' / 'pr-review').mkdir(parents=True)
                (root / 'home').mkdir()
                for stage in ('reviewer-1', 'reviewer-2'):
                    box = isolation.IndependentSandbox(run, stage, 'codex')
                    try:
                        script = ('test ! -e "$SECRET_REPORT" && test ! -e "$ORIGINAL_TREE/source" '
                                  '&& test ! -e /tmp/review-private/previous '
                                  '&& test ! -e /proc/1/root && ! touch "$HOST_WRITE" 2>/dev/null '
                                  '&& test "$(cat source)" = "snapshot at PR head" '
                                  '&& printf finding > /tmp/review-private/previous')
                        argv, env = box.wrap(['/bin/sh', '-c', script], dict(os.environ,
                            SECRET_REPORT=str(state / 'other-review.md'), ORIGINAL_TREE=str(repo),
                            HOST_WRITE=str(root / 'host-write')))
                        self.assertNotIn('GIT_DIR', env)
                        result = subprocess.run(argv, env=env, text=True, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        box.verify()
                    finally:
                        box.close()
                box = isolation.IndependentSandbox(run, 'reviewer-1', 'codex')
                try:
                    # A reviewer-controlled Git filter must not run on the host
                    # during post-review integrity verification.
                    (box.checkout / '.gitattributes').write_text('source filter=evil\n')
                    (box.checkout / 'source').write_text('modified')
                    marker = root / 'host-filter-ran'
                    filter_command = f'touch /tmp/review-private/checkout/filter-ran; touch {marker}; cat'
                    git_env = {key: value for key, value in os.environ.items() if key != 'GIT_DIR'}
                    subprocess.run(['git', '-C', str(box.checkout), 'config', 'filter.evil.clean', filter_command],
                                   env=git_env, check=True)
                    with self.assertRaisesRegex(Exception, 'changed its isolated snapshot'):
                        box.verify()
                    self.assertTrue((box.checkout / 'filter-ran').exists())
                    self.assertFalse(marker.exists())
                finally:
                    box.close()
                (root / 'home/.codex').mkdir()
                (root / 'home/.codex/auth.json').symlink_to(state / 'other-review.md')
                with self.assertRaisesRegex(Exception, 'overlaps protected review files'):
                    isolation.IndependentSandbox(run, 'reviewer-1', 'codex')

    def test_missing_isolation_fails_closed(self):
        with patch('pr_review.isolation.shutil.which', return_value=None):
            with self.assertRaisesRegex(Exception, 'require bubblewrap'):
                isolation.IndependentSandbox(None, 'reviewer-1', 'codex')

    def test_slot_reports_are_produced_and_reused_independently(self):
        if not shutil.which('bwrap'):
            self.skipTest('bubblewrap unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state, scratch = root / 'repo', root / 'state', root / 'scratch'
            for path in (repo, state, scratch, state / 'reviews', state / 'logs', state / 'prompts', root / 'home', root / 'xdg' / 'pr-review'):
                path.mkdir(parents=True, exist_ok=True)
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.name', 'Test')
            self.git(repo, 'config', 'user.email', 'test@example.com')
            (repo / 'source').write_text('snapshot')
            self.git(repo, 'add', 'source')
            self.git(repo, 'commit', '-qm', 'snapshot')
            head = self.git(repo, 'rev-parse', 'HEAD')
            self.git(repo, 'update-ref', 'refs/review/head', head)
            self.git(repo, 'update-ref', 'refs/review/base', head)
            def git(*args, cwd=None, check=True):
                return subprocess.run(['git', *args], cwd=cwd or repo, check=check, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            run = Review.__new__(Review)
            run.path = run.root = state
            run.worktree, run.scratch = repo, scratch
            run.repo = SimpleNamespace(common=repo / '.git', head_ref='refs/review/head', base_ref='refs/review/base', git=git, verify=Mock())
            run.settings = settings.validate({'reviewer_slots': [
                {'id': 'reviewer-1', 'provider': 'claude', 'model': 'one', 'effort': 'low'},
                {'id': 'reviewer-2', 'provider': 'codex', 'model': 'two', 'effort': 'high'}]})
            run.manifest = {'snapshot': {'head': head, 'base': head, 'merge_base': head}, 'phases': {}}
            run.tools, run.runtime, run.active = {'claude': 'mock', 'codex': 'mock'}, providers.Runtime(), None
            run.save = Mock()
            run.begin, run.reset_publication = Mock(), Mock()
            def command(stage):
                if stage == 'reviewer-1':
                    return ['/bin/sh', '-c', 'mkdir -p __pycache__; printf data > __pycache__/generated; ln -s /etc/passwd generated-link; printf \'%s\\n\' \'{"type":"result","subtype":"success","is_error":false,"result":"first finding"}\'']
                return ['/bin/sh', '-c', 'if [ "${FAIL_SLOT:-}" = 1 ]; then exit 42; fi; printf "second finding\\n" > "$1"; printf \'%s\\n\' \'{"type":"review.completed"}\'',
                        'review', str(state / 'reviews/reviewer-2.md.tmp')]
            run.command = command
            with patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'xdg'), 'HOME': str(root / 'home'),
                                         'CODEX_HOME': str(root / 'home/.codex'),
                                         'CLAUDE_CONFIG_DIR': str(root / 'home/.claude')}):
                pipeline.independent(run, 'reviewer-1')
                archive = Path(run.manifest['phases']['reviewer-1']['generated_archive'])
                self.assertEqual((archive / 'files/__pycache__/generated').read_text(), 'data')
                self.assertIn('generated-link', (archive / 'symlinks.json').read_text())
                self.assertFalse((repo / '__pycache__').exists())
                with patch.dict(os.environ, {'FAIL_SLOT': '1'}), self.assertRaisesRegex(Exception, 'reviewer-2 failed'):
                    pipeline.independent(run, 'reviewer-2')
                first_fingerprint = run.manifest['phases']['reviewer-1']['fingerprint']
                pipeline.prepare_slots(run, run.settings['reviewer_slots'])
                pipeline.independent(run, 'reviewer-2')
                self.assertEqual(run.manifest['phases']['reviewer-1']['fingerprint'], first_fingerprint)
                self.assertEqual((state / 'reviews/reviewer-1.md').read_text(), 'first finding\n')
                self.assertEqual((state / 'reviews/reviewer-2.md').read_text(), 'second finding\n')
                with patch.object(run, 'model', side_effect=AssertionError('valid work must be reused')):
                    pipeline.independent(run, 'reviewer-1')
                    pipeline.independent(run, 'reviewer-2')


class SlotRecoveryTest(unittest.TestCase):
    def test_switching_to_followup_clears_slot_phases_and_restart_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Store.__new__(Store)
            run.root = run.path = Path(directory)
            run.resuming = True
            run.manifest = {'analysis_kind': 'full', 'phases': {
                'reviewer-1': {'status': 'completed'}, 'reviewer-2': {'status': 'completed'},
                'synthesis': {'status': 'completed'}}, 'slot_restart': {'stages': ['reviewer-1']}}
            run.begin, run.reset_directories, run.save = Mock(), Mock(), Mock()
            run.scope('followup', ['thread:one'])
            self.assertNotIn('reviewer-1', run.manifest['phases'])
            self.assertNotIn('reviewer-2', run.manifest['phases'])
            self.assertNotIn('slot_restart', run.manifest)
            self.assertEqual(run.manifest['analysis_kind'], 'followup')

    def test_followup_catalog_requires_only_processor(self):
        config = settings.validate({'reviewer_slots': [
            {'id': 'reviewer-1', 'provider': 'claude', 'model': 'reviewer', 'effort': 'high'}],
            'processing': {'provider': 'codex', 'model': 'processor', 'effort': 'low'}})
        catalogs = {'codex': {'provider': 'codex', 'models': [{'value': 'processor', 'efforts': ['low']}]}}
        models.validate_selection(config, catalogs, reviewers=False)
        with self.assertRaisesRegex(Exception, 'Refresh claude'):
            models.validate_selection(config, catalogs)
        for malformed in (None, [], {'codex': []}, {'codex': {'provider': 'codex', 'models': [None]}},
                          {'codex': {'provider': 'codex', 'models': [{'value': 'processor', 'efforts': None}]}}):
            with self.subTest(catalogs=malformed), self.assertRaises(Exception) as error:
                models.validate_selection(config, malformed, reviewers=False)
            self.assertEqual(error.exception.code, 'invalid_request')

    def test_adaptation_uses_processor_model_and_effort(self):
        for provider in ('claude', 'codex'):
            choice = {'provider': provider, 'model': 'processing-model', 'effort': 'low'}
            args = providers.adaptation_command(settings.validate({'processing': choice}))
            self.assertIn('processing-model', args)
            self.assertIn('low' if provider == 'claude' else 'model_reasoning_effort=low', args)

    def test_changed_slot_preserves_peer_and_archives_downstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ('reviews', 'prompts', 'logs'):
                (root / folder).mkdir()
            for name, value in [('reviewer-1.md', 'valid first'), ('reviewer-2.md', 'old second'),
                                ('synthesis.json', 'old synthesis')]:
                (root / 'reviews' / name).write_text(value)
            manifest = {'phases': {'reviewer-1': {'status': 'completed'},
                                   'reviewer-2': {'status': 'completed'},
                                   'synthesis': {'status': 'completed'}}}
            run = SimpleNamespace(path=root, manifest=manifest, reset_publication=Mock(), begin=Mock(), save=Mock())
            slots = [{'id': 'reviewer-1'}, {'id': 'reviewer-2'}]
            with patch('pr_review.pipeline.cache.inputs', return_value={}), \
                 patch('pr_review.pipeline.cache.reusable', side_effect=lambda run, stage, output, expected: stage == 'reviewer-1'):
                pipeline.prepare_slots(run, slots)
            self.assertEqual((root / 'reviews/reviewer-1.md').read_text(), 'valid first')
            self.assertFalse((root / 'reviews/reviewer-2.md').exists())
            self.assertNotIn('reviewer-2', manifest['phases'])
            self.assertNotIn('synthesis', manifest['phases'])
            archive = Path(manifest['review_history'][-1]['archive'])
            self.assertEqual((archive / 'reviews/reviewer-2.md').read_text(), 'old second')
            self.assertEqual((archive / 'reviews/synthesis.json').read_text(), 'old synthesis')

    def test_restart_journal_rejects_symlinked_artifact_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir, outside = root / 'run', root / 'outside'
            run_dir.mkdir()
            outside.mkdir()
            (outside / 'reviewer-1.md').write_text('keep')
            (run_dir / 'reviews').symlink_to(outside, target_is_directory=True)
            archive = run_dir / 'restart-history' / 'slots-one'
            archive.mkdir(parents=True)
            run = SimpleNamespace(path=run_dir, save=Mock(), manifest={'phases': {}, 'slot_restart': {
                'stages': ['reviewer-1'], 'files': ['reviews/reviewer-1.md'], 'archive': str(archive)}})
            with self.assertRaisesRegex(Exception, 'Symlink in slot restart'):
                pipeline.prepare_slots(run, [{'id': 'reviewer-1'}])
            self.assertEqual((outside / 'reviewer-1.md').read_text(), 'keep')

    def test_legacy_report_is_copied_only_after_original_identity_verifies(self):
        from pr_review.storage import hash_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'reviews').mkdir()
            report = root / 'reviews/claude-independent.md'
            report.write_text('legacy finding')
            slot = {'id': 'reviewer-1', 'provider': 'claude', 'model': 'old-model', 'effort': 'high'}
            mapping = {'reviewer-1': {'phase': 'claude', 'output': 'reviews/claude-independent.md',
                        'provider': 'claude', 'model': 'old-model', 'effort': 'high'}}
            run = SimpleNamespace(path=root, pending=root, save=Mock(),
                manifest={'legacy_review_artifacts': mapping,
                          'phases': {'claude': {'status': 'completed', 'fingerprint': 'legacy-fingerprint',
                                               'output_hash': hash_file(report)}}})
            with patch('pr_review.pipeline.cache.inputs', return_value={'identity': 'verified'}), \
                 patch('pr_review.pipeline.cache.reusable', return_value=False):
                pipeline.adopt_legacy_slots(run, [slot])
            self.assertNotIn('reviewer-1', run.manifest['phases'])
            with patch('pr_review.pipeline.cache.inputs', return_value={'identity': 'verified'}), \
                 patch('pr_review.pipeline.cache.reusable', return_value=True):
                pipeline.adopt_legacy_slots(run, [slot])
            self.assertEqual(report.read_text(), 'legacy finding')
            self.assertEqual((root / 'reviews/reviewer-1.md').read_text(), 'legacy finding')
            self.assertEqual(run.manifest['phases']['reviewer-1']['legacy_source']['fingerprint'], 'legacy-fingerprint')


if __name__ == '__main__':
    unittest.main()
