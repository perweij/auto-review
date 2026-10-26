"""Automatic completion against real Git, with providers and GitHub mocked."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import patch

import test_owner_commit_plan
from pr_review import owner_auto_commit, owner_publication, owner_providers
from pr_review.common import ApiError
from pr_review.owner_cli import main as owner_cli
from pr_review.owner_state import OwnerSession
from pr_review.processes import Cancelled


class AutoCommitTest(unittest.TestCase):
    def setUp(self):
        self.group = test_owner_commit_plan.CommitPlanTest()
        self.group.setUp()
        self.addCleanup(self.group.doCleanups)
        self.fixture = self.group.fixture
        self.request, self.git, self.keys = self.group.request, self.group.git, self.group.keys

    def start(self):
        return self.request("owner-fix", items=[self.keys[0], "thread:T1", self.keys[1], "thread:T2"], auto_commit=True)

    def resume(self, **fields):
        batch = self.request("owner-read")["batches"][-1]
        return self.request("owner-fix", batch=batch["id"], **fields)

    def hook(self, name, script):
        path = Path(self.git("rev-parse", "--git-path", "hooks/" + name))
        if not path.is_absolute():
            path = self.fixture.repo / path
        path.write_text("#!/bin/sh\nset -eu\n" + script + "\n")
        path.chmod(0o755)
        return path

    def push(self, sha):
        self.git("update-ref", "refs/pull/7/head", sha)
        self.fixture.metadata["headRefOid"] = sha

    def test_group_commits_after_final_review_and_stages_precise_replies_without_push(self):
        invoke = owner_providers.invoke
        real = owner_auto_commit.run
        counts = []
        def commit(*args, **kwargs):
            counts.append(invoke.call_count)
            return real(*args, **kwargs)
        with patch("pr_review.owner_auto_commit.run", side_effect=commit):
            view = self.start()
        batch = view["batches"][-1]
        self.assertEqual(counts, [6, 6])
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["commit_execution"]["status"], "complete")
        commits = batch["commit_progress"]["commits"]
        self.assertEqual(len(commits), 2)
        self.assertEqual(self.git("show", commits[0] + ":source.txt"), "first fix")
        self.assertEqual(self.git("show", commits[1] + ":source.txt"), "first fix\nsecond fix")
        for key, sha in zip(self.keys, commits):
            item = next(i for i in view["feedback"] if i["key"] == key)
            self.assertEqual(item["local_completion"]["commit"], sha)
        for reply, sha, target in zip(view["replies"], commits, [101, 102]):
            self.assertEqual(reply["commits"], [sha])
            self.assertEqual(reply["target"], {"kind": "reply", "root_id": target})
            self.assertEqual(reply["status"], "staged")
            self.assertTrue(reply["awaiting_push"])
            self.assertIn("https://github.com/org/repo/commit/" + sha, reply["body"])
        self.assertFalse(self.fixture.posts)
        self.assertEqual(self.fixture.metadata["headRefOid"], self.fixture.head)
        self.assertFalse(self.git("status", "--porcelain", cwd=view["worktree"]))
        calls = invoke.call_count
        with patch("pr_review.owner_providers.preflight", side_effect=AssertionError("Commit recovery must not need provider access")):
            again = self.resume()
        self.assertEqual(again["batches"][-1]["commit_progress"]["commits"], commits)
        self.assertEqual([r["id"] for r in again["replies"]], [r["id"] for r in view["replies"]])
        self.assertEqual(invoke.call_count, calls)

    def test_publication_keeps_pending_selection_and_never_silently_omits_unpushed_replies(self):
        view = self.start()
        commits = view["batches"][-1]["commit_progress"]["commits"]
        self.push(commits[0])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-prepare")
        self.assertEqual(caught.exception.code, "unpublished_commit")
        view = self.request("owner-read")
        self.assertEqual([r["status"] for r in view["replies"]], ["staged", "staged"])
        self.assertEqual([r["awaiting_push"] for r in view["replies"]], [False, True])
        self.assertFalse(self.fixture.posts)
        self.push(commits[1])
        preview = self.request("owner-prepare")
        self.assertEqual(len(preview["replies"]), 2)
        self.assertFalse(self.fixture.posts)
        published = self.request("owner-publish", token=preview["token"])
        self.assertTrue(all(r["status"] == "published" for r in published["replies"]))
        self.assertEqual([p[1] for p in self.fixture.posts], ["comments/101/replies", "comments/102/replies"])

    def test_dropped_pushed_commit_and_changed_discussion_block_the_whole_preview(self):
        view = self.start()
        commits = view["batches"][-1]["commit_progress"]["commits"]
        self.push(commits[-1])
        self.request("owner-prepare")
        self.push(commits[0])
        with self.assertRaises(ApiError):
            self.request("owner-prepare")
        self.assertEqual(self.request("owner-read")["replies"][1]["status"], "unstaged")
        self.fixture.feedback[0]["comments"][0]["body"] = "Updated concern"
        with self.assertRaises(ApiError):
            self.request("owner-prepare")
        self.assertFalse(self.fixture.posts)

    def test_publishing_first_reply_does_not_invalidate_the_other_verified_item(self):
        view = self.start()
        self.push(view["batches"][-1]["commit_progress"]["commits"][-1])
        preview = self.request("owner-prepare")
        original = self.fixture.publish
        def publish(session, endpoint, payload=None, **kwargs):
            receipt = original(session, endpoint, payload, **kwargs)
            if payload and "101/replies" in endpoint:
                self.fixture.feedback[0]["comments"].append({"id": receipt["id"], "author": "author", "body": payload["body"],
                                                          "url": receipt["html_url"], "created_at": "today", "updated_at": "today"})
            return receipt
        with patch("pr_review.owner_publication.gh", side_effect=publish):
            published = self.request("owner-publish", token=preview["token"])
        self.assertEqual([r["status"] for r in published["replies"]], ["published", "published"])

    def test_retry_recovers_commit_created_before_result_was_saved(self):
        real = owner_auto_commit.run
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise Cancelled(signal.SIGTERM)
        with patch("pr_review.owner_auto_commit.run", side_effect=interrupted):
            with self.assertRaises(Cancelled):
                self.start()
        view = self.request("owner-read")
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["commit_execution"]["attempts"][0]["status"], "uncertain")
        calls = owner_providers.invoke.call_count
        first = self.git("rev-parse", "HEAD", cwd=view["worktree"])
        self.push(first)  # A push of our own prefix is a valid resume source.
        resumed = self.resume()
        batch = resumed["batches"][-1]
        self.assertEqual(batch["commit_progress"]["commits"][0], first)
        self.assertEqual(len(batch["commit_progress"]["commits"]), 2)
        self.assertTrue(batch["commit_execution"]["attempts"][0]["recovered"])
        self.assertEqual(len(resumed["replies"]), 2)
        self.assertEqual(owner_providers.invoke.call_count, calls)

    def test_hook_failure_keeps_verified_staging_and_remembers_mode_on_resume(self):
        hook = self.hook("pre-commit", "echo 'hook rejected this commit' >&2\nexit 1")
        view = self.start()
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["commit_execution"]["status"], "paused")
        self.assertIn("hook rejected", batch["commit_execution"]["error"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), batch["baseline"])
        self.assertEqual(self.git("write-tree", cwd=view["worktree"]), batch["commit_plan"]["units"][0]["tree"])
        calls = owner_providers.invoke.call_count
        manual = self.resume(auto_commit=False)
        self.assertFalse(manual["batches"][-1]["auto_commit"])
        hook.write_text("#!/bin/sh\nexit 0\n")
        done = self.resume(auto_commit=True)
        self.assertEqual(done["batches"][-1]["commit_execution"]["status"], "complete")
        self.assertEqual(owner_providers.invoke.call_count, calls)

    def test_hook_changing_committed_code_is_retained_for_inspection_not_claimed_fixed(self):
        self.hook("pre-commit", "printf 'unverified hook code\\n' > source.txt\ngit add source.txt")
        view = self.start()
        batch = view["batches"][-1]
        self.assertTrue(batch["commit_execution"]["inspection_required"])
        head = self.git("rev-parse", "HEAD", cwd=view["worktree"])
        self.assertNotEqual(head, batch["baseline"])
        self.assertEqual(self.git("show", head + ":source.txt"), "unverified hook code")
        self.assertFalse(view["replies"])
        self.assertFalse(any(i.get("local_completion") for i in view["feedback"]))
        self.resume()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), head)

    def test_cancelled_commit_with_hook_source_edits_requires_inspection_on_recovery(self):
        self.hook("post-commit", "printf 'hook edit\\n' >> source.txt")
        real = owner_auto_commit.run
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise Cancelled(signal.SIGTERM)
        with patch("pr_review.owner_auto_commit.run", side_effect=interrupted):
            with self.assertRaises(Cancelled):
                self.start()
        view = self.resume()
        self.assertTrue(view["batches"][-1]["commit_execution"]["inspection_required"])
        self.assertFalse(view["replies"])
        self.assertFalse(any(i.get("local_completion") for i in view["feedback"]))

    def test_hook_timeout_and_signing_failure_never_bypass_configuration(self):
        hook = self.hook("pre-commit", "sleep 10")
        with patch.dict(os.environ, {"PR_REVIEW_COMMIT_TIMEOUT": "1"}):
            view = self.start()
        self.assertIn("timed out", view["batches"][-1]["commit_execution"]["error"])
        hook.write_text("#!/bin/sh\nexit 0\n")
        signer = self.fixture.root / "signer"
        signer.write_text("#!/bin/sh\necho 'signing needs attention' >&2\nexit 1\n")
        signer.chmod(0o755)
        self.git("config", "commit.gpgsign", "true")
        self.git("config", "gpg.program", str(signer))
        view = self.resume()
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), view["batches"][-1]["baseline"])

    def test_real_ssh_signing_is_honored(self):
        import shutil
        if not shutil.which("ssh-keygen"):
            self.skipTest("SSH signing requires ssh-keygen")
        key = self.fixture.root / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
        allowed = self.fixture.root / "allowed-signers"
        allowed.write_text("test@example.com " + key.with_suffix(".pub").read_text())
        self.git("config", "gpg.format", "ssh")
        self.git("config", "user.signingkey", str(key))
        self.git("config", "gpg.ssh.allowedSignersFile", str(allowed))
        self.git("config", "commit.gpgsign", "true")
        view = self.start()
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "complete")
        for sha in view["batches"][-1]["commit_progress"]["commits"]:
            self.git("verify-commit", sha)

    def test_existing_human_reply_and_explicit_unstaging_survive_retries(self):
        self.request("owner-mutate", operation="reply", item="thread:T1", body="My own explanation")
        view = self.start()
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")
        self.assertEqual(len(view["replies"]), 2)
        self.assertEqual(view["replies"][0]["body"], "My own explanation")
        reply = view["replies"][1]
        self.request("owner-mutate", operation="edit", reply=reply["id"], body="Human wording for second fix")
        again = self.resume()
        self.assertEqual(len(again["replies"]), 2)
        self.assertEqual(again["replies"][1]["body"], "Human wording for second fix")
        self.assertEqual(again["replies"][1]["status"], "unstaged")
        self.assertEqual(again["batches"][-1]["commit_progress"]["completed"], 2)
        restaged = self.request("owner-mutate", operation="stage", reply=reply["id"])
        self.assertEqual(restaged["replies"][1]["status"], "staged")
        self.assertTrue(restaged["replies"][1]["awaiting_push"])

    def test_explicit_reassociation_can_recover_an_automatic_reply_after_rebase(self):
        view = self.start()
        batch = view["batches"][-1]
        rebased = self.git("commit-tree", batch["final_tree"], "-p", self.fixture.base, "-m", "Manually rebased fix")
        self.git("update-ref", "refs/heads/" + view["branch"], rebased)
        self.push(rebased)
        view = self.request("owner-associate", batch=batch["id"], items=["thread:T1"], commits=[rebased])
        reply = view["replies"][0]
        self.assertFalse(reply.get("automatic"))
        self.assertEqual(reply["status"], "unstaged")
        self.request("owner-mutate", operation="acknowledge-reply", reply=reply["id"])
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        preview = self.request("owner-prepare")
        self.assertEqual([r["id"] for r in preview["replies"]], [reply["id"]])
        self.assertEqual(preview["replies"][0]["commits"], [rebased])

    def test_resume_repairs_missing_replies_after_all_commits_and_keeps_unstaging(self):
        view = self.start()
        session = OwnerSession({"session": self.fixture.path})
        state = session.load()
        first = state["replies"][0]
        state["replies"] = [first]
        first["status"] = "unstaged"
        state["batches"][-1]["commit_execution"]["status"] = "running"
        session.save(state)
        calls = owner_providers.invoke.call_count
        done = self.resume()
        self.assertEqual(len(done["replies"]), 2)
        self.assertEqual(done["replies"][0]["status"], "unstaged")
        self.assertEqual(done["batches"][-1]["commit_progress"]["commits"], view["batches"][-1]["commit_progress"]["commits"])
        self.assertEqual(owner_providers.invoke.call_count, calls)

    def test_combined_fallback_and_manual_ready_batch_opt_in(self):
        self.group.repair = True
        self.group.fix()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.group.worktree), self.group.batch["baseline"])
        view = self.resume(auto_commit=True)
        self.assertEqual(view["batches"][-1]["commit_plan"]["mode"], "combined")
        self.assertEqual(view["batches"][-1]["commit_progress"]["total"], 1)
        self.assertEqual(view["replies"][0]["commits"], view["replies"][1]["commits"])

    def test_automatic_commits_preserve_later_manual_edits_and_conflicting_index(self):
        self.group.fix()
        source = self.group.worktree / "source.txt"
        source.write_text(source.read_text() + "later manual edit\n")
        self.git("add", "source.txt", cwd=self.group.worktree)
        before = self.git("write-tree", cwd=self.group.worktree)
        view = self.resume(auto_commit=True)
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")
        self.assertEqual(self.git("write-tree", cwd=self.group.worktree), before)
        self.git("restore", "--staged", "source.txt", cwd=self.group.worktree)
        view = self.resume()
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "complete")
        self.assertIn("later manual edit", self.git("diff", cwd=self.group.worktree))

    def test_unrelated_head_change_between_commits_stops_and_retains_prefix(self):
        real = owner_auto_commit.run
        count = 0
        def commit(*args, **kwargs):
            nonlocal count
            result = real(*args, **kwargs)
            count += 1
            if count == 1:
                self.git("commit", "--allow-empty", "-qm", "unexpected external commit", cwd=kwargs["cwd"])
            return result
        with patch("pr_review.owner_auto_commit.run", side_effect=commit):
            view = self.start()
        self.assertEqual(count, 1)
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")
        self.assertTrue(view["batches"][-1]["commit_execution"]["inspection_required"])
        self.assertFalse(view["replies"])

    def test_missing_destination_retains_commits_and_retry_can_prepare_replies(self):
        original = owner_publication.draft_fixed
        def missing(session, state, batch, **kwargs):
            target = state["push_target"]["repository"]
            state["push_target"]["repository"] = ""
            try:
                return original(session, state, batch, **kwargs)
            finally:
                state["push_target"]["repository"] = target
        with patch("pr_review.owner_publication.draft_fixed", side_effect=missing):
            view = self.start()
        self.assertEqual(view["batches"][-1]["commit_progress"]["completed"], 2)
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")
        self.assertFalse(view["replies"])
        calls = owner_providers.invoke.call_count
        view = self.resume()
        self.assertEqual(len(view["replies"]), 2)
        self.assertEqual(owner_providers.invoke.call_count, calls)

    def test_single_local_fix_and_no_change_never_invent_replies_or_empty_commits(self):
        with patch("pr_review.owner_providers.invoke", side_effect=self.fixture.model):
            view = self.request("owner-fix", items=[self.keys[0]], auto_commit=True)
        self.assertEqual(view["batches"][-1]["commit_progress"]["total"], 1)
        self.assertFalse(view["replies"])
        head = self.git("rev-parse", "HEAD", cwd=view["worktree"])
        original = self.fixture.model
        def no_change(session, state, batch, role, context):
            value = original(session, state, batch, role, context)
            if role != "plan":
                for outcome in value["outcomes"]:
                    outcome["status"] = "already_addressed"
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=no_change):
            view = self.request("owner-fix", items=[self.keys[1]], auto_commit=True)
        self.assertEqual(view["batches"][-1]["commit_progress"]["total"], 0)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), head)
        self.assertFalse(view["replies"])

    def test_timeline_destination_and_uncertain_reply_are_preserved(self):
        self.fixture.feedback[0]["kind"] = "comment"
        self.fixture.feedback[0].pop("can_reply")  # Timeline feedback has no thread permission field.
        self.request("owner-refresh")
        view = self.start()
        reply = view["replies"][0]
        self.assertEqual(reply["target"], {"kind": "timeline"})
        self.assertIn("In response to [this feedback]", reply["body"])
        session = OwnerSession({"session": self.fixture.path})
        state = session.load()
        state["actions"].append({"reply": reply["id"], "status": "ambiguous"})
        session.save(state)
        view = self.resume()
        self.assertEqual(view["replies"][0], reply)
        self.assertEqual(len(view["replies"]), 2)
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "paused")

    def test_invalid_options_are_rejected_before_any_provider(self):
        for fields in ({"auto_commit": "yes"}, {"auto_commit": True, "verify_only": True}):
            with self.assertRaises(ApiError):
                self.request("owner-fix", items=[self.keys[0]], **fields)
        with patch.dict(os.environ, {"PR_REVIEW_COMMIT_TIMEOUT": "0"}):
            with self.assertRaises(ApiError):
                self.start()
        self.assertEqual(owner_providers.invoke.call_count, 0)

    def test_provider_pause_retains_auto_mode_and_resume_does_not_repeat_successful_fix(self):
        original = self.group.model
        incomplete = True
        roles = []
        def model(session, state, batch, role, context):
            nonlocal incomplete
            roles.append(role)
            value = original(session, state, batch, role, context)
            if role == "review" and incomplete:
                incomplete = False
                value.update(complete=False, summary="Verification needs another pass")
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.start()
            self.assertEqual(view["batches"][-1]["status"], "paused")
            self.assertTrue(view["batches"][-1]["auto_commit"])
            self.assertFalse(view["replies"])
            view = self.resume()
        self.assertEqual(roles.count("fix"), 2)
        self.assertEqual(view["batches"][-1]["commit_execution"]["status"], "complete")

    def test_remote_change_after_combined_review_still_invalidates_verification(self):
        original = self.group.model
        def model(session, state, batch, role, context):
            value = original(session, state, batch, role, context)
            if role == "review" and context.get("scope"):
                sha = self.git("commit-tree", batch["baseline"] + "^{tree}", "-p", batch["baseline"], "-m", "remote advance")
                self.push(sha)
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            with self.assertRaises(ApiError) as caught:
                self.start()
        self.assertEqual(caught.exception.code, "stale_source")
        view = self.request("owner-read")
        self.assertEqual(view["batches"][-1]["status"], "paused")
        self.assertNotIn("commit_execution", view["batches"][-1])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), view["batches"][-1]["baseline"])

    def test_cli_commit_mode_is_explicit_and_can_enable_a_verified_batch(self):
        self.group.fix()
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
            code = owner_cli(["resume", "--session", self.fixture.path, "--batch", self.group.batch["id"], "--auto-commit"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["batches"][-1]["commit_execution"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
