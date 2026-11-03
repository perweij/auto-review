"""Helpers for the independent acceptance checks. Never shown to a model workspace.

Each check script is run as ``python check_<task>.py CHECKOUT`` from a directory
outside the checkout and prints one JSON object on its last line:
``{"acceptance": {id: bool}, "regression": {id: bool}}``.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TIMEOUT = 60


class Harness:
    def __init__(self, checkout):
        self.checkout = checkout
        self.directory = tempfile.mkdtemp(prefix="ledger-check-")
        self.data = os.path.join(self.directory, "ledger.json")
        self.acceptance, self.regression = {}, {}

    def cli(self, *argv, data=None, extra_env=None):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": self.checkout}
        env.pop("LEDGER_DATA", None)
        env.update(extra_env or {})
        done = subprocess.run([sys.executable, "-m", "ledger", "--data", data or self.data, *argv], cwd=self.directory,
                              env=env, text=True, capture_output=True, timeout=TIMEOUT)
        return done.returncode, done.stdout, done.stderr

    def fresh(self):
        for name in os.listdir(self.directory):
            path = os.path.join(self.directory, name)
            if os.path.isfile(path):
                os.unlink(path)

    def check(self, kind, name, function):
        try:
            outcome = bool(function())
        except Exception as exc:  # a crashing check is a failed check, never a skipped one
            print(f"check {name} raised {exc!r}", file=sys.stderr)
            outcome = False
        getattr(self, kind)[name] = outcome

    def acceptance_check(self, name, function):
        self.check("acceptance", name, function)

    def regression_check(self, name, function):
        self.check("regression", name, function)

    def common_regressions(self):
        def original_tests():
            # The pristine tests run against the candidate package. They are copied beside nothing that
            # contains a ``ledger`` package (unittest puts its top-level directory first on sys.path, so
            # discovering inside the fixture would test the frozen baseline instead), and the candidate
            # is asserted to be the package that was imported.
            fixture_tests = os.environ["LEDGER_ORIGINAL_TESTS"]
            pristine = os.path.join(self.directory, "pristine-tests")
            shutil.rmtree(pristine, ignore_errors=True)
            shutil.copytree(fixture_tests, pristine, ignore=shutil.ignore_patterns("__pycache__"))
            program = ("import os, sys, unittest, ledger\n"
                       "assert os.path.realpath(ledger.__file__).startswith(os.path.realpath(sys.argv[1]) + os.sep), ledger.__file__\n"
                       "suite = unittest.defaultTestLoader.discover(sys.argv[2])\n"
                       "sys.exit(not unittest.TextTestRunner(verbosity=0).run(suite).wasSuccessful())\n")
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": self.checkout}
            done = subprocess.run([sys.executable, "-c", program, self.checkout, pristine],
                                  cwd=self.directory, env=env, text=True, capture_output=True, timeout=120)
            if done.returncode:
                print(done.stderr[-2000:], file=sys.stderr)
            return done.returncode == 0
        def repo_tests():
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=self.checkout, env=env,
                                  text=True, capture_output=True, timeout=120)
            return done.returncode == 0
        self.regression_check("original_tests_pass", original_tests)
        self.regression_check("repository_tests_pass", repo_tests)

    def finish(self):
        print(json.dumps({"acceptance": self.acceptance, "regression": self.regression}))
