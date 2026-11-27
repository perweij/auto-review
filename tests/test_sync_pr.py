import argparse
import contextlib
import io
import json
import os
import re
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "sync-pr"
cli = runpy.run_path(str(SCRIPT))
sync = cli["sync"]
Refusal = cli["Refusal"]


class ReviewerSelectionTests(unittest.TestCase):
    def test_distinct_models_can_share_effort(self):
        pairs = cli["reviewers"](["gpt-6.2-sol:high", "gpt-6-astra:high"])
        self.assertEqual(pairs, [["gpt-6.2-sol", "high"], ["gpt-6-astra", "high"]])

    def test_same_model_with_different_efforts_is_rejected(self):
        with self.assertRaisesRegex(Refusal, "distinct model IDs"):
            cli["reviewers"](["gpt-6.2-sol:medium", "gpt-6.2-sol:high"])


class SyncTests(unittest.TestCase):
    CODEX = """
import subprocess
output = Path(sys.argv[sys.argv.index('-o')+1])
prompt = sys.stdin.read()
flag = lambda name: (root / name).exists()
if '--json' in sys.argv:
    resumed = 'resume' in sys.argv
    (root / 'solver-prompt').write_text(prompt)
    assert '--ephemeral' not in sys.argv
    if resumed:
        assert sys.argv[-2:] == ['session-421', '-']
    Path('text').write_text('ONE\\nTWO\\nthree\\n' + ('fixed\\n' if resumed else ''))
    if flag('add-file'):
        Path('brand-new').write_text('new')
    subprocess.run(['git', 'add', '-A'], check=True)
    command = ['git', 'commit', '--no-edit']
    if resumed:
        command.append('--amend')
    subprocess.run(command, check=True, stdout=subprocess.PIPE)
    print(json.dumps(dict(type='thread.started', thread_id='session-421')))
    answer = dict(checks=[dict(project='.', argv=['true'], reason='smoke', source='fixture')],
                  unavailable_checks=[] if resumed else [dict(project='.', argv=['false'], reason='needs database', source='fixture')],
                  decision_needed=None)
else:
    model = sys.argv[sys.argv.index('--model')+1]
    (root / model).write_text(json.dumps([sys.argv, prompt]))
    with (root / 'calls').open('a') as calls:
        print(model, file=calls)
    if model == 'first' and flag('move-base'):
        subprocess.run(['git', '--git-dir', str(root/'remote.git'), 'update-ref',
                        'refs/heads/main', 'refs/heads/topic'], check=True)
    if model == 'first' and flag('race'):
        subprocess.run(['git', '--git-dir', str(root/'remote.git'), 'update-ref',
                        'refs/heads/topic', 'refs/heads/main'], check=True)
    if model == 'first' and flag('amend'):
        subprocess.run(['git', '-C', str(root/'retained'), 'commit', '--amend', '-qm', 'changed'], check=True)
    if model == 'first' and flag('fail-first') and not flag('retry'):
        output.write_bytes(b'{"truncated"')
        sys.exit(0)
    response = root / ('response-' + model)
    if response.exists():
        output.write_text(response.read_text())
        sys.exit(0)
    corrected = '+fixed' in prompt
    blockers = []
    if model == 'first' and flag('reject-first') and (not corrected or flag('always-reject')):
        blockers = [dict(category='lost_side_change', path='text', symbol='', explanation='base line one is missing',
                         evidence='remerge diff')]
    followups = [dict(kind='edge_case', text='resume under a Latin-1 locale')] if model == 'second' and corrected else []
    answer = dict(verdict='reject' if blockers else 'approve', reason='checked', blockers=blockers, followups=followups)
output.write_text(json.dumps(answer))
"""
    REJECT = json.dumps(dict(verdict="reject", reason="incompatible", followups=[], blockers=[dict(
        category="lost_side_change", path="text", symbol="", explanation="incompatible", evidence="fixture")]))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "work"
        self.repo.mkdir()
        env = patch.dict(os.environ, PATH=str(self.root) + ":" + os.environ["PATH"],
                         GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"), TEST_ROOT=tmp.name)
        env.start()
        self.addCleanup(env.stop)
        # The wrapper isolates Git's environment. HOME supplies identity in its temporary clone.
        home = patch.dict(os.environ, HOME=tmp.name, GIT_CONFIG_GLOBAL=str(self.root / ".gitconfig"))
        home.start()
        self.addCleanup(home.stop)
        self.git("config", "--global", "user.name", "Test")
        self.git("config", "--global", "user.email", "test@example.org")
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.write("one\ntwo\nthree\n")
        self.git("branch", "topic")
        self.write("ONE\ntwo\nthree\n")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "topic")
        self.write("one\nTWO\nthree\n")
        self.head = self.git("rev-parse", "HEAD")
        self.git("push", str(self.remote), "main", "topic")
        self.meta()
        self.stub("gh", """
if sys.argv[1:3] == ['pr', 'view']:
    print('https://github.com/org/repo/pull/421')
elif 'graphql' in sys.argv:
    own = not (root / 'foreign').exists()
    print(json.dumps({'data': {'viewer': {'login': 'me'}, 'repository': {'pullRequest':
          {'viewerDidAuthor': own, 'author': {'login': 'me' if own else 'other'}}}}}))
elif sys.argv[1] == 'api':
    print((root / 'meta').read_text())
elif sys.argv[1:3] == ['pr', 'comment']:
    with (root / 'comments').open('a') as comments:
        comments.write(json.dumps([sys.argv[3:], sys.stdin.read()]) + '\\n')
else:
    assert sys.argv[1:] == ['pr', 'update-branch', 'https://github.com/org/repo/pull/421', '--rebase']
    (root / 'updated').touch()
    if not (root / 'clean').exists():
        sys.exit('X Cannot update PR branch due to conflicts')
""")
        self.stub("codex", self.CODEX)
        profile = patch.dict(sync.__globals__, read_profile=lambda _: dict(profile=None, project={}))
        profile.start()
        self.addCleanup(profile.stop)
        tmpdir = patch.object(tempfile, "tempdir", tmp.name)
        tmpdir.start()
        self.addCleanup(tmpdir.stop)
        self.args = argparse.Namespace(pr=421, force=False, repo="org/repo", reviewer=["first:low", "second:high"])

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True,
                              text=True).stdout.strip()

    def write(self, content):
        (self.repo / "text").write_text(content)
        self.git("add", "text")
        self.git("commit", "-m", "edit")

    def meta(self):
        def side(ref, sha):
            return {"repo": {"clone_url": str(self.remote)}, "ref": ref, "sha": sha}
        (self.root / "meta").write_text(json.dumps(dict(state="open", base=side("main", self.base),
                                                      head=side("topic", self.head))))

    def stub(self, name, body):
        tool = self.root / name
        tool.write_text("#!/usr/bin/env python3\nimport sys, os, json\nfrom pathlib import Path\n"
                        "root = Path(os.environ['TEST_ROOT'])\n" + body)
        tool.chmod(0o755)

    def tip(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", "topic")

    def test_foreign_pr_requires_explicit_override(self):
        (self.root / "foreign").touch()
        (self.root / "clean").touch()
        with self.assertRaisesRegex(Refusal, "author: other, signed in: me"):
            sync(self.args)
        self.assertFalse((self.root / "updated").exists())
        self.args.force = True
        sync(self.args)
        self.assertTrue((self.root / "updated").exists())

    def test_force_still_requires_review_approvals(self):
        self.args.force = True
        (self.root / "foreign").touch()
        (self.root / "response-second").write_text(self.REJECT)
        with self.assertRaisesRegex(Refusal, "two approvals required"):
            sync(self.args)
        self.assertEqual(self.tip(), self.head)

    def test_cli_resolves_conflicts_through_pr_review(self):
        command = [str(SCRIPT), "--reviewer", "first:low",
                   "--reviewer", "second:high", "421"]
        self.stub("profile-cli", "sys.exit('environment consulted')")
        with patch.dict(os.environ, SYNC_PR_PR_REVIEW=str(self.root / "profile-cli")):
            result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True,
                                    env=dict(os.environ, TMPDIR=str(self.root)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment consulted", result.stderr)
        self.assertEqual(self.tip(), self.head)
        for removed in ("--no-repair", "--strategy=replay"):
            result = subprocess.run(command + [removed], cwd=self.repo, capture_output=True, text=True)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_clean(self):
        (self.root / "clean").touch()
        sync(self.args)
        self.assertFalse((self.root / "first").exists())

    def test_clean_no_push_skips_github_update_and_local_push(self):
        from contextlib import nullcontext
        self.git("checkout", "main")
        (self.repo / "extra").write_text("clean")
        self.git("add", "extra")
        self.git("commit", "-m", "clean topic")
        self.git("push", "--force", str(self.remote), "HEAD:topic")
        self.head = self.git("rev-parse", "HEAD")
        self.meta()
        self.args.no_push = True
        checkout = self.root / "retained"
        checkout.mkdir()
        with patch.dict(sync.__globals__, sync_directory=lambda *_: nullcontext(str(checkout))):
            sync(self.args)
        self.assertEqual(self.git("--git-dir", str(checkout / ".git"), "rev-parse", "HEAD^{tree}"),
                         self.git("rev-parse", self.head + "^{tree}"))
        self.assertFalse((self.root / "updated").exists())
        self.assertFalse((self.root / "first").exists())
        self.assertEqual(self.tip(), self.head)

    def test_clean_rebase_of_linear_pr(self):
        self.git("checkout", "topic")
        self.git("reset", "--hard", "topic^")
        (self.repo / "extra").write_text("clean")
        self.git("add", "extra")
        self.git("commit", "-m", "topic edits extra only")
        self.git("push", "--force", str(self.remote), "topic")
        self.head = self.git("rev-parse", "HEAD")
        self.meta()
        sync(self.args)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "topic^"), self.base)
        self.assertFalse((self.root / "first").exists())

    def test_no_push_reports_preflight_failure_before_checkout(self):
        (self.root / "foreign").touch()
        command = [str(SCRIPT), "--no-push", "421"]
        result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No checkout was created", result.stderr)

    def test_publish_retained_checkout(self):
        checkout, patches = self.merge_trial()
        fork = self.root / "fork.git"
        self.git("clone", "--bare", str(self.remote), str(fork))
        meta = json.loads((self.root / "meta").read_text())
        meta["head"]["repo"]["clone_url"] = str(fork)
        (self.root / "meta").write_text(json.dumps(meta))
        output = io.StringIO()
        with patches, contextlib.redirect_stdout(output):
            sync(self.args)
        self.assertIn(f"Publish with: {shlex.join([sys.argv[0], '--push', str(checkout)])}\n", output.getvalue())
        record = json.loads((checkout / ".git/sync-pr-ready.json").read_text())
        self.stub("codex", "sys.exit('must not call providers during push')")
        def publish(*arguments, cwd=None):
            return subprocess.run([str(SCRIPT), "--push", *arguments],
                                  cwd=cwd or self.repo, capture_output=True, text=True)
        self.assertIn("No completed", publish(cwd=self.repo).stderr)
        (checkout / "dirty").touch()
        self.assertIn("local changes", publish(str(checkout)).stderr)
        (checkout / "dirty").unlink()
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", self.head)
        self.assertIn("Base changed", publish(str(checkout)).stderr)
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", self.base)
        self.git("--git-dir", str(fork), "update-ref", "refs/heads/topic", self.base)
        self.assertNotEqual(publish(str(checkout)).returncode, 0)
        self.git("--git-dir", str(fork), "update-ref", "refs/heads/topic", self.head)
        result = publish(str(checkout))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("--git-dir", str(fork), "rev-parse", "topic"), record["candidate"])
        self.assertEqual(self.tip(), self.head)
        meta["head"]["sha"] = record["candidate"]
        (self.root / "meta").write_text(json.dumps(meta))
        self.assertIn("Already pushed", publish(cwd=checkout).stdout)
        self.assertEqual(record["followups"], [])
        self.assertFalse((self.root / "comments").exists())

    def test_stale_pr_base_metadata(self):
        current_base = self.base
        self.base = self.git("rev-parse", "main^")
        self.meta()
        sync(self.args)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", "topic^2"), current_base)

    def test_base_moves_during_review(self):
        (self.root / "move-base").touch()
        with self.assertRaisesRegex(Refusal, "Base branch main moved"):
            sync(self.args)
        self.assertEqual(self.tip(), self.head)

    def test_approved(self):
        sync(self.args)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", "topic:text"), "ONE\nTWO\nthree")
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", "-s", "--format=%P", "topic"),
                         f"{self.head} {self.base}")
        for model, effort in (("first", "low"), ("second", "high")):
            argv, prompt = json.loads((self.root / model).read_text())
            self.assertIn('model_reasoning_effort="' + effort + '"', argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "danger-full-access")
            self.assertIn("+TWO", prompt)

    def test_reviewers_can_inspect_candidate_dependencies(self):
        self.git("checkout", "main")
        (self.repo / "helper.py").write_text("def current_helper(): pass\n")
        self.git("add", "helper.py")
        self.git("commit", "-m", "add target helper")
        self.git("push", str(self.remote), "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "topic")
        _, patches = self.merge_trial()
        with patches:
            sync(self.args)
        prompt = json.loads((self.root / "first").read_text())[1]
        git_dir = re.search(r"git --git-dir=(\S+)", prompt)[1]
        candidate = re.search(r"candidate: merge commit ([0-9a-f]{40})", prompt)[1]
        self.assertIn("current_helper", self.git("--git-dir", git_dir, "show", candidate + ":helper.py"))

    def test_review_failure(self):
        for result in ("invalid", '{"verdict":"uncertain","reason":"unknown","followups":[],"blockers":[]}',
                       self.REJECT):
            with self.subTest(result=result):
                (self.root / "response-second").write_text(result)
                with self.assertRaisesRegex(Refusal, "two approvals required"):
                    sync(self.args)
                self.assertEqual(self.tip(), self.head)

    def test_reviewer_execution_failures(self):
        for failure in (Refusal("unavailable model"), subprocess.TimeoutExpired("codex", 300)):
            with patch.dict(sync.__globals__, run=Mock(side_effect=failure)):
                verdict, reason = cli["review_resolution"](("first", "low"), "Check", self.evidence())
            self.assertEqual(verdict, "error")
            self.assertTrue(reason)

    def evidence(self):
        return dict(git_dir=str(self.root), candidate_commit="c" * 40, pr_head=self.head, base=self.base,
                    merge_base=self.base, conflicted=["text"], remerge_diff="diff", verification=None,
                    unavailable_checks=[])

    def test_only_timed_out_slot_retries_once(self):
        pairs = [("first", "low"), ("second", "high")]
        original_run = cli["run"]
        evidence = self.evidence()
        for recover in (True, False):
            calls = {"first": [], "second": []}

            def timed_run(command, cwd, data, **options):
                model = command[command.index("--model") + 1]
                deadline = options["timeout"]
                calls[model].append(deadline)
                if model == "first" and (deadline == 1800 or not recover):
                    (cwd / "response").write_bytes(b"partial")
                    raise subprocess.TimeoutExpired(command, deadline)
                return original_run(command, cwd, data, **options)

            with patch.dict(sync.__globals__, run=timed_run):
                answers = cli["review_candidate"](pairs, evidence, "candidate")
                self.assertEqual([a[0] for a in answers], ["approve" if recover else "error", "approve"])
                self.assertEqual(calls, {"first": [1800, 3600], "second": [1800]})
                if not recover:
                    recover = True
                    resumed = cli["review_candidate"](pairs, evidence, "retained", answers)
                    self.assertEqual([a[0] for a in resumed], ["approve", "approve"])
                    self.assertEqual(calls, {"first": [1800, 3600, 3600], "second": [1800]})
        records = [json.loads(f.read_text()) for f in self.root.glob("review-output-*/reviewer-1/retry/timing.json")]
        self.assertEqual(sorted(r["outcome"] for r in records), ["exit 0", "timeout"])
        self.assertTrue(all(r["timeout_seconds"] == 3600 and r["elapsed_seconds"] >= 0 for r in records))

    def merge_trial(self, *flags):
        from contextlib import nullcontext
        self.args.no_push = True
        checkout = self.root / "retained"
        checkout.mkdir()
        (self.repo / "later").write_text("later")
        self.git("add", "later")
        self.git("commit", "-m", "second topic commit")
        self.git("push", str(self.remote), "topic")
        self.head = self.git("rev-parse", "HEAD")
        self.meta()
        for flag in flags:
            (self.root / flag).touch()
        return checkout, patch.dict(sync.__globals__, sync_directory=lambda *_: nullcontext(str(checkout)))

    def calls(self):
        path = self.root / "calls"
        calls = path.read_text().splitlines() if path.exists() else []
        return calls.count("first"), calls.count("second")

    def test_branch_merge_retry_resumes_solver_and_posts_followups_on_publish(self):
        checkout, patches = self.merge_trial("fail-first", "reject-first")
        with patches:
            with self.assertRaisesRegex(Refusal, "two approvals required"):
                sync(self.args)
            self.assertIn(b'{"truncated"', [p.read_bytes() for p in
                                            (checkout / ".git").glob("review-output-*/*/response.json")])
            receipt = checkout / ".git/sync-pr-retry.json"
            saved = json.loads(receipt.read_text())
            self.assertEqual((saved["session"], saved["gate"]), ("session-421", "resolution-v1"))
            self.assertIn("remerge CONFLICT", saved["evidence"]["remerge_diff"])
            (self.root / "retry").touch()
            cli["retry_command"](checkout, self.args)
        self.assertFalse(receipt.exists())
        self.assertIn("adapt the PR code to the base", (self.root / "solver-prompt").read_text())
        self.assertIn("base line one is missing", (self.root / "solver-prompt").read_text())
        prompt = json.loads((self.root / "first").read_text())[1]
        self.assertIn("needs database", prompt)
        self.assertIn("remerge CONFLICT", prompt)
        self.assertIn("Files with textual conflicts:\ntext\n", prompt)
        self.assertEqual(self.git("-C", str(checkout), "show", "-s", "--format=%P"), f"{self.head} {self.base}")
        self.assertEqual(self.tip(), self.head)
        self.assertEqual(self.calls(), (3, 2))
        record = json.loads((checkout / ".git/sync-pr-ready.json").read_text())
        self.assertEqual(record["followups"], [dict(kind="edge_case", text="resume under a Latin-1 locale",
                                                    reviewers=["second/high"])])
        self.assertIn("Latin-1", (checkout / ".git/sync-pr-followups.md").read_text())
        self.assertFalse((self.root / "comments").exists())
        cli["publish_prepared"](checkout, False)
        candidate = self.git("-C", str(checkout), "rev-parse", "HEAD")
        self.assertEqual(self.tip(), candidate)
        comments = [json.loads(line) for line in (self.root / "comments").read_text().splitlines()]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][0], ["https://github.com/org/repo/pull/421", "--body-file", "-"])
        self.assertIn(candidate, comments[0][1])
        self.assertIn("- resume under a Latin-1 locale (second/high)", comments[0][1])
        meta = json.loads((self.root / "meta").read_text())
        meta["head"]["sha"] = candidate
        (self.root / "meta").write_text(json.dumps(meta))
        cli["publish_prepared"](checkout, False)
        self.assertEqual(len((self.root / "comments").read_text().splitlines()), 1)

    def test_second_resolution_rejection_is_terminal(self):
        checkout, patches = self.merge_trial("reject-first", "always-reject")
        with patches, self.assertRaisesRegex(Refusal, "rejected the corrected merge(.|\\n)*base line one is missing"):
            sync(self.args)
        self.assertEqual(self.calls(), (2, 2))
        self.assertEqual(self.tip(), self.head)
        self.assertFalse((checkout / ".git/sync-pr-ready.json").exists())
        self.assertFalse((checkout / ".git/sync-pr-retry.json").exists())

    def test_resolution_adding_files_absent_from_both_parents_is_refused_before_review(self):
        _, patches = self.merge_trial("add-file")
        with patches, self.assertRaisesRegex(Refusal, "absent from both parents: brand-new"):
            sync(self.args)
        self.assertEqual(self.calls(), (0, 0))
        self.assertEqual(self.tip(), self.head)

    def test_oversized_resolution_is_refused(self):
        def git(*args, check=True):
            return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=check)
        self.git("checkout", "topic")
        git("merge", "main", check=False)
        self.write("ONE\nTWO\nthree\n")
        candidate = self.git("rev-parse", "HEAD")
        remerge = git("show", "--remerge-diff", "--format=", candidate).stdout.decode()
        cli["resolution_scope"](git, self.head, self.base, candidate, ["text"], remerge)
        with patch.dict(cli["resolution_scope"].__globals__, MAX_RESOLUTION_LINES=3):
            with self.assertRaisesRegex(Refusal, "remerge diff has [0-9]+ lines \\(limit 3\\)"):
                cli["resolution_scope"](git, self.head, self.base, candidate, ["text"], remerge)

    def test_resolution_review_validates_verdicts_and_findings(self):
        blocker = dict(category="lost_side_change", path="text", symbol="", explanation="dropped", evidence="diff")
        followup = dict(kind="scope", text="extra resume hash")
        cases = (("reject", [blocker], [], "reject"), ("reject", [], [], "error"), ("approve", [blocker], [], "error"),
                 ("reject", [dict(blocker, category="edge_case")], [], "error"),
                 ("approve", [], [dict(followup, kind="nit")], "error"), ("approve", [], [followup], "approve"))
        for verdict, blockers, followups, expected in cases:
            (self.root / "response-first").write_text(json.dumps(dict(verdict=verdict, reason="checked",
                                                                      blockers=blockers, followups=followups)))
            with self.subTest(verdict=verdict, blockers=blockers, followups=followups):
                self.assertEqual(cli["review_resolution"](("first", "low"), "", self.evidence())[0], expected)
        prompt = json.loads((self.root / "first").read_text())[1]
        self.assertIn("Precedence. The base is the shared truth", prompt)
        self.assertIn("Files with textual conflicts:\ntext\n", prompt)

    def test_retry_receipt_from_older_gate_is_refused(self):
        with self.assertRaisesRegex(Refusal, "older sync-pr"):
            cli["branch_merge"](Mock(), self.root, {}, self.head, self.base, {}, dict(strategy="replay"))

    def test_merge_candidate_requires_pinned_parents_and_clean_checkout(self):
        def git(*args):
            return subprocess.run(['git',*args],cwd=self.repo,capture_output=True,check=True)
        with self.assertRaisesRegex(Refusal,'clean local merge'):
            cli['merge_candidate'](git,self.head,self.base)
        self.git('merge','-s','ours','--no-edit','main')
        cli['merge_candidate'](git,self.head,self.base)
        (self.repo/'untracked').touch()
        with self.assertRaisesRegex(Refusal,'clean local merge'):
            cli['merge_candidate'](git,self.head,self.base)

    def test_candidate_change(self):
        _, patches = self.merge_trial("amend")
        with patches, self.assertRaisesRegex(Refusal, "candidate changed"):
            sync(self.args)
        self.assertEqual(self.tip(), self.head)

    def test_racing_push(self):
        (self.root / "race").touch()
        with self.assertRaises(Refusal):
            sync(self.args)
        self.assertEqual(self.tip(), self.base)


if __name__ == "__main__":
    unittest.main()
