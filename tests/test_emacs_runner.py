"""Exercise the real Emacs runner against small checkout/installed fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMACS = shutil.which(os.environ.get('EMACS', 'emacs'))


@unittest.skipUnless(EMACS, 'Emacs is not installed; run check-emacs in a frontend development environment')
class EmacsRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        version = subprocess.run([EMACS, '--quick', '--batch', '--eval', '(princ emacs-major-version)'],
                                 text=True, capture_output=True, check=True, timeout=15)
        if int(version.stdout) < 29:
            raise unittest.SkipTest('Frontend checks require Emacs 29.1 or newer')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emacs runner ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / 'checkout'
        self.source = self.checkout / 'emacs'
        self.tests = self.source / 'tests'
        self.tests.mkdir(parents=True)
        for name in ('run.sh', 'bootstrap.el'):
            shutil.copyfile(ROOT / 'emacs/tests' / name, self.tests / name)
        modules = {
            'magit-auto-review': '(defun magit-auto-review () "Fixture entry." \'checkout)',
            'magit-auto-review-setup': '(defun magit-auto-review--default-settings () "Fixture settings." \'checkout)',
            'magit-auto-review-owner': '(defun magit-auto-review-owner-open () "Fixture owner." \'checkout)',
        }
        for name, code in modules.items():
            (self.source / f'{name}.el').write_text(f';;; Fixture -*- lexical-binding: t; -*-\n{code}\n(provide \'{name})\n')
        (self.tests / 'magit-auto-review-tests.el').write_text('''
(require 'ert)
(require 'magit-auto-review)
(require 'magit-auto-review-setup)
(ert-deftest fixture/checkout-bytecode ()
  (should (eq (magit-auto-review) 'checkout))
  (should (eq (magit-auto-review--default-settings) 'checkout))
  (should (file-in-directory-p (symbol-file 'magit-auto-review 'defun)
                               (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR"))))
''')
        (self.tests / 'magit-auto-review-owner-tests.el').write_text('''
(require 'magit-auto-review-owner)
(ert-deftest fixture/owner ()
  (should (eq (magit-auto-review-owner-open) 'checkout)))
''')
        self.installed = self.root / 'installed'
        self.installed.mkdir()
        self.foreign = self.installed / 'magit-auto-review-setup.el'
        self.foreign.write_text("(defun magit-auto-review--default-settings () 'installed)\n"
                                "(provide 'magit-auto-review-setup)\n")
        self.site = self.root / 'site'
        self.site.mkdir()
        self.site_marker = self.root / 'site-loaded'
        (self.site / 'site-start.el').write_text(
            f'(with-temp-file {json.dumps(str(self.site_marker))} (insert "loaded"))\n'
            f'(load {json.dumps(str(self.foreign))} nil t)\n')
        self.dependency_file = self.root / 'dependencies.el'
        self.phases = self.root / 'phases'
        self.dependency_file.write_text(
            '(unless load-prefer-newer (error "Dependency source preference changed"))\n'
            f'(write-region (concat (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR") "\\n") nil '
            f'{json.dumps(str(self.phases))} t \'silent)\n'
            # An unloaded installed package earlier on load-path must not win.
            f'(add-to-list \'load-path {json.dumps(str(self.installed))})\n')
        self.env = dict(os.environ, EMACS=EMACS, EMACSLOADPATH=str(self.site) + os.pathsep,
                        AUTO_REVIEW_TEST_LOAD_FILE=str(self.dependency_file))

    def run_runner(self):
        return subprocess.run(['bash', str(self.tests / 'run.sh')], cwd=self.root, env=self.env,
                              text=True, capture_output=True, timeout=30)

    def test_site_start_cannot_contaminate_either_process(self):
        # Prove that the stale site-start really executes without the runner's flag.
        control = subprocess.run([EMACS, '--batch', '--eval',
                                  "(princ (magit-auto-review--default-settings))"],
                                 env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn('installed', control.stdout)
        self.assertTrue(self.site_marker.exists())
        self.site_marker.unlink()
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.site_marker.exists())
        self.assertIn('2 results as expected', result.stderr)
        phases = self.phases.read_text().splitlines()
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[0], phases[1])
        self.assertFalse(Path(phases[0]).exists(), 'Temporary bytecode must be cleaned up')
        self.assertFalse(list(self.source.glob('*.elc')), 'Compilation must not write into the checkout')

    def check_preload_rejected(self, only_ert):
        with self.dependency_file.open('a') as stream:
            load = f'(load {json.dumps(str(self.foreign))} nil t)'
            if only_ert:
                load = ('(when (directory-files (getenv "AUTO_REVIEW_TEST_BYTECODE_DIR") nil "\\\\.elc$") '
                        + load + ')')
            stream.write(load + '\n')
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Auto-review test contamination:', result.stderr)
        self.assertIn(str(self.foreign), result.stderr)
        self.assertIn('AUTO_REVIEW_TEST_LOAD_FILE must load only dependencies', result.stderr)
        phases = self.phases.read_text().splitlines()
        self.assertEqual(len(phases), 2 if only_ert else 1)
        self.assertFalse(Path(phases[0]).exists())
        self.assertNotIn('Running 2 tests', result.stderr)

    def test_dependency_preload_is_rejected_before_compilation(self):
        self.check_preload_rejected(only_ert=False)

    def test_dependency_preload_is_rejected_in_the_fresh_ert_process(self):
        self.check_preload_rejected(only_ert=True)


if __name__ == '__main__':
    unittest.main()
