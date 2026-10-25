"""Owner workflows against real Git, with external GitHub/providers replaced."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review.api import dispatch
from pr_review.common import ApiError, locked
from pr_review.repository import Repository
from pr_review.owner_state import OwnerSession
from pr_review import owner_fixes, owner_git, owner_providers, owner_publication
from pr_review.providers import Runtime
from pr_review.owner_cli import main as owner_cli
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

REAL_INVOKE = owner_providers.invoke


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "-q", "-b", "main")
        (self.repo / "source.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "source.txt").write_text("feature\n")
        self.git("commit", "-qam", "feature")
        self.head = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", "https://github.com/org/repo.git")
        self.git("config", "url." + str(self.repo) + ".insteadOf", "https://github.com/org/repo.git")
        self.git("update-ref", "refs/pull/7/head", self.head)
        self.metadata = {"number": 7, "title": "Feature", "url": "https://github.com/org/repo/pull/7", "state": "OPEN",
                         "headRefOid": self.head, "baseRefOid": self.base, "baseRefName": "main", "headRefName": "feature",
                         "author": {"login": "author"}}
        self.feedback = [self.item("thread:T1", 101), self.item("thread:T2", 102)]
        self.calls = []
        self.posts = []
        self.receipts = []
        for target, side_effect in (
            ("pr_review.pulls.github_json", self.github),
            ("pr_review.discussion.fetch", lambda repo: ({"items": copy.deepcopy(self.feedback)}, {"inline": []})),
            ("pr_review.owner_providers.preflight", lambda session, state: None),
            ("pr_review.owner_providers.invoke", self.model),
            ("pr_review.owner_publication.gh", self.publish),
        ):
            p = patch(target, side_effect=side_effect)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(Repository, "metadata", side_effect=lambda *a, **k: copy.deepcopy(self.metadata))
        p.start()
        self.addCleanup(p.stop)
        self.view = dispatch({"protocol": 1, "command": "owner-open", "cwd": str(self.repo), "pr": 7})
        self.path = self.view["session"]
        self.request("owner-refresh")

    @staticmethod
    def item(key, identity, kind="thread"):
        return {"key": key, "kind": kind, "root_id": identity, "url": f"https://github.com/org/repo/pull/7#discussion_r{identity}",
                "path": "source.txt", "line": 1, "original_line": 1, "is_resolved": False, "is_outdated": False,
                "can_reply": True, "can_resolve": True, "can_unresolve": True,
                "comments": [{"id": identity, "author": "reviewer", "body": "Please fix this",
                              "url": f"https://github.com/org/repo/pull/7#discussion_r{identity}",
                              "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}]}

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, text=True, capture_output=True, check=True).stdout.strip()

    def github(self, repository, endpoint, cwd, **kwargs):
        if endpoint == "user":
            return {"login": "author"}
        return {"number": 7, "html_url": self.metadata["url"], "head": {"sha": self.metadata["headRefOid"],
                "ref": "feature", "repo": {"html_url": "https://github.com/org/repo"}}}

    def request(self, command, **fields):
        view = dispatch({"protocol": 1, "command": "owner-read", "session": self.path})
        result = dispatch({"protocol": 1, "command": command, "session": self.path, "revision": view["revision"], **fields})
        self.view = result.get("owner", result)
        return result

    def model(self, session, state, batch, role, context):
        self.calls.append((role, context))
        keys = [i["key"] for i in context["feedback"]]
        if role == "plan":
            return {"tasks": [{"id": "shared", "items": keys, "depends_on": [], "rationale": "Shared implementation", "expected": [{"item": k, "outcome": "Concern addressed"} for k in keys]}]}
        if role == "fix":
            (Path(state["worktree"]) / "source.txt").write_text("fixed\n")
            (Path(state["worktree"]) / "new.txt").write_text("new test fixture\n")
            batch["checkpoint"] = owner_git.checkpoint(session, state, "fake provider")
        return {"summary": "Verified fix", "outcomes": [{"item": k, "status": "fixed", "evidence": "Checked the new behavior"} for k in keys],
                "complete": True, "checks": ["focused test"], "blocking": []}

    def publish(self, session, endpoint, payload=None, pages=False, resource="pulls"):
        if pages:
            return copy.deepcopy(self.receipts)
        self.posts.append((resource, endpoint, payload))
        identity = len(self.posts) + 1000
        value = {"id": identity, "body": payload["body"], "user": {"login": "author"},
                 "html_url": f"https://github.com/org/repo/pull/7#issuecomment-{identity}"}
        if resource == "pulls":
            value["in_reply_to_id"] = int(endpoint.split("/")[1])
        self.receipts.append(value)
        return value

    def fix(self, **fields):
        return self.request("owner-fix", items=[i["key"] for i in self.feedback], **fields)

    def commit_fix(self, push=True):
        worktree = self.view["worktree"]
        self.git("add", ".", cwd=worktree)
        self.git("commit", "-qm", "Address feedback", cwd=worktree)
        sha = self.git("rev-parse", "HEAD", cwd=worktree)
        if push:
            self.git("update-ref", "refs/pull/7/head", sha)
            self.metadata["headRefOid"] = sha
        return sha

    def test_owner_open_is_local_and_hidden_from_reviewer_discovery(self):
        with patch.object(Repository, "metadata", side_effect=AssertionError("network")):
            result = dispatch({"protocol": 1, "command": "owner-open", "cwd": str(self.repo), "pr": 7})
        self.assertEqual(result["session"], self.path)
        result = dispatch({"protocol": 1, "command": "discover", "cwd": str(self.repo)})
        self.assertEqual(result["sessions"], [])

    def test_private_owner_artifacts_cannot_be_created_inside_a_worktree(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.repo / "private-state")}), self.assertRaises(ApiError) as caught:
            dispatch({"protocol": 1, "command": "owner-open", "cwd": str(self.repo), "pr": 7})
        self.assertEqual(caught.exception.code, "invalid_session")
        self.assertFalse((self.repo / "private-state").exists())

    def test_selection_remembered_with_inferred_or_explicit_repository(self):
        dispatch({"protocol": 1, "command": "toolkit", "cwd": str(self.repo), "toolkit": "owner"})
        result = dispatch({"protocol": 1, "command": "toolkit", "cwd": str(self.repo), "repository": "github.com/org/repo"})
        self.assertEqual(result["toolkit"], "owner")

    def test_initial_settings_use_custom_defaults_without_reviewer_history(self):
        request = {"protocol": 1, "command": "owner-open", "cwd": str(self.repo), "pr": 8,
                   "defaults": {"processing_provider": "codex", "codex_model": "chosen", "codex_effort": "low"}}
        view = dispatch(request)
        self.assertEqual(view["settings"]["fix"], {"provider": "codex", "model": "chosen", "effort": "low"})
        self.assertEqual(view["settings"]["fix"], view["settings"]["review"])
        request["defaults"]["codex_model"] = "different"
        self.assertEqual(dispatch(request)["settings"]["fix"]["model"], "chosen")

    def test_cli_session_actions_share_the_application_and_cancel_uses_owned_id(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(owner_cli(["--session", self.path, "triage", "--operation", "defer", "--item", "thread:T1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["feedback"][0]["triage"], "deferred")
        with patch("pr_review.owner_cli.dispatch", return_value={"cancelled": True}) as call, redirect_stdout(StringIO()):
            self.assertEqual(owner_cli(["cancel", "--operation-id", "owned-operation"]), 0)
        self.assertEqual(call.call_args.args[0]["command"], "cancel")
        self.assertEqual(call.call_args.args[0]["operation_id"], "owned-operation")

    def test_cli_refuses_conflicting_pr_and_session_selectors(self):
        before = self.request("owner-read")
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            self.assertEqual(owner_cli(["8", "triage", "--session", self.path, "--operation", "defer", "--item", "thread:T1"]), 1)
        self.assertIn("differs from the selected owner session", errors.getvalue())
        self.assertEqual(self.request("owner-read"), before)

    def test_stale_mutation_does_not_overwrite_triage(self):
        revision = self.view["revision"]
        self.request("owner-mutate", operation="defer", items=["thread:T1"])
        with self.assertRaises(ApiError) as caught:
            dispatch({"protocol": 1, "command": "owner-mutate", "session": self.path, "revision": revision,
                      "operation": "restore", "items": ["thread:T1"]})
        self.assertEqual(caught.exception.code, "stale_revision")

    def test_changed_feedback_restores_deferred_and_outdated_is_unresolved(self):
        self.request("owner-mutate", operation="defer", items=["thread:T1"])
        self.feedback[0]["comments"][0]["body"] = "Updated feedback"
        self.feedback[0]["is_outdated"] = True
        view = self.request("owner-refresh")
        item = view["feedback"][0]
        self.assertTrue(item["attention"])
        self.assertTrue(item["is_outdated"])
        self.assertFalse(item["is_resolved"])

    def test_failed_refresh_keeps_last_complete_snapshot(self):
        before = self.request("owner-read")
        with patch("pr_review.discussion.fetch", side_effect=ApiError("offline", "Offline")):
            with self.assertRaises(ApiError):
                self.request("owner-refresh")
        self.assertEqual(self.request("owner-read"), before)

    def test_group_fix_combines_items_and_never_changes_real_index_or_head(self):
        view = self.fix()
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(len(batch["tasks"]), 1)
        self.assertEqual(batch["verified_items"], ["thread:T1", "thread:T2"])
        self.assertEqual([role for role, _ in self.calls], ["plan", "fix", "review", "review"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), self.head)
        self.assertEqual(self.git("diff", "--cached", cwd=view["worktree"]), "")
        self.assertEqual(self.git("rev-parse", batch["baseline_ref"]), self.head)
        self.assertEqual(self.git("rev-parse", batch["checkpoint"]["ref"]), batch["checkpoint"]["tree"])
        self.assertTrue(Path(view["worktree"], "new.txt").exists())
        self.assertFalse(self.posts)

    def test_verify_only_never_calls_fixer_even_for_blocking_findings(self):
        original = self.model
        def model(*args):
            value = original(*args)
            if args[3] == "review":
                value["blocking"] = ["Still broken"]
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.fix(verify_only=True)
        self.assertEqual(view["batches"][-1]["status"], "paused")
        self.assertNotIn("fix", [r for r, _ in self.calls])

    def test_one_automatic_task_repair_then_pause(self):
        original = self.model
        def model(*args):
            value = original(*args)
            if args[3] == "review":
                value["blocking"] = ["Still broken"]
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.fix()
        self.assertEqual([r for r, _ in self.calls].count("fix"), 2)
        self.assertEqual(view["batches"][-1]["status"], "paused")
        self.assertEqual(view["batches"][-1]["tasks"][0]["repairs"], 1)

    def test_combined_review_has_one_repair_limit_and_preserves_task_reports(self):
        original = self.model
        def model(*args):
            result = original(*args)
            if args[3] == "review" and args[4].get("scope"):
                result["blocking"] = ["Interaction still needs correction"]
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.fix()
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "paused")
        self.assertEqual(batch["repairs"], 1)
        self.assertTrue(batch["tasks"][0]["review"]["complete"])
        self.assertEqual([role for role, _ in self.calls].count("fix"), 2)
        self.assertFalse(self.posts)

    def test_interrupted_partial_task_requires_explicit_adoption(self):
        original = self.model
        def model(*args):
            value = original(*args)
            if args[3] == "fix":
                raise ApiError("cancelled", "Interrupted")
            return value
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            with self.assertRaises(ApiError):
                self.fix()
        state = self.request("owner-read")
        batch = state["batches"][-1]
        self.assertEqual(batch["status"], "interrupted")
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "interrupted_task")
        self.calls.clear()
        view = self.request("owner-fix", batch=batch["id"], adopt=True)
        self.assertEqual(view["batches"][-1]["status"], "ready")
        self.assertNotIn("fix", [r for r, _ in self.calls])

    def test_replan_keeps_original_baseline_and_accumulated_changes(self):
        self.fix()
        batch = self.view["batches"][-1]
        self.feedback[0]["comments"][0]["body"] = "Clarified requirement"
        self.calls.clear()
        view = self.request("owner-fix", batch=batch["id"], replan=True)
        plan = next(c for role, c in self.calls if role == "plan")
        self.assertIn("+fixed", plan["batch_diff"])
        self.assertEqual(view["batches"][-1]["baseline"], self.head)

    def test_exact_commit_is_staged_only_after_push_and_replies_to_both_threads(self):
        self.fix()
        sha = self.commit_fix(push=False)
        view = self.request("owner-associate")
        self.assertEqual(view["replies"], [])
        self.git("update-ref", "refs/pull/7/head", sha)
        self.metadata["headRefOid"] = sha
        view = self.request("owner-associate")
        self.assertEqual(len(view["replies"]), 2)
        self.assertTrue(all(r["status"] == "staged" for r in view["replies"]))
        self.assertFalse(self.posts)
        preview = self.request("owner-prepare")
        result = self.request("owner-publish", token=preview["token"])
        self.assertTrue(all(r["status"] == "published" for r in result["replies"]))
        self.assertEqual([p[1] for p in self.posts], ["comments/101/replies", "comments/102/replies"])

    def test_extra_changes_require_explicit_association(self):
        self.fix()
        Path(self.view["worktree"], "other.txt").write_text("manual addition\n")
        sha = self.commit_fix()
        self.assertFalse(self.request("owner-associate")["replies"])
        view = self.request("owner-associate", batch=self.view["batches"][-1]["id"], commits=[sha], items=["thread:T1"])
        self.assertEqual(len(view["replies"]), 1)

    def test_resuming_after_commit_preserves_verified_tree_for_association(self):
        self.fix()
        batch = self.view["batches"][-1]
        self.commit_fix()
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "stale_source")
        self.assertEqual(len(self.request("owner-associate")["replies"]), 2)

    def test_split_commits_need_explicit_association_even_with_exact_final_tree(self):
        self.fix()
        worktree = self.view["worktree"]
        batch = self.view["batches"][-1]
        self.git("add", "source.txt", cwd=worktree)
        self.git("commit", "-qm", "First half", cwd=worktree)
        first = self.git("rev-parse", "HEAD", cwd=worktree)
        second = self.commit_fix()
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}", cwd=worktree), batch["final_tree"])
        self.assertFalse(self.request("owner-associate")["replies"])
        view = self.request("owner-associate", batch=batch["id"], commits=[first, second])
        self.assertTrue(all(r["commits"] == [first, second] for r in view["replies"]))
        self.assertEqual(len(view["replies"]), 2)

    def test_amendment_reassociates_but_preserves_human_wording_unstaged(self):
        self.fix()
        first = self.commit_fix()
        self.request("owner-associate")
        reply = self.view["replies"][0]
        self.request("owner-mutate", operation="edit", reply=reply["id"], body="My explanation")
        worktree = self.view["worktree"]
        self.git("commit", "--amend", "-qm", "Amended exact fix", cwd=worktree)
        second = self.git("rev-parse", "HEAD", cwd=worktree)
        self.assertNotEqual(first, second)
        self.git("update-ref", "refs/pull/7/head", second)
        self.metadata["headRefOid"] = second
        view = self.request("owner-associate")
        self.assertEqual(view["replies"][0]["body"], "My explanation")
        self.assertEqual(view["replies"][0]["status"], "unstaged")
        self.assertEqual(view["replies"][0]["commits"], [second])
        self.assertTrue(view["replies"][0]["needs_attention"])

    def test_force_push_unstages_reply_and_blocks_publication(self):
        self.fix()
        self.commit_fix()
        self.request("owner-associate")
        self.git("update-ref", "refs/pull/7/head", self.head)
        self.metadata["headRefOid"] = self.head
        view = self.request("owner-refresh")
        self.assertTrue(all(r["status"] == "unstaged" for r in view["replies"]))
        with self.assertRaises(ApiError):
            self.request("owner-prepare")
        self.assertFalse(self.posts)

    def test_timeline_reply_uses_issue_endpoint(self):
        self.feedback = [self.item("comment:501", 501, kind="comment")]
        self.request("owner-refresh")
        self.request("owner-mutate", operation="reply", item="comment:501", body="Thanks for clarifying.")
        reply = self.view["replies"][-1]
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        prepared = self.request("owner-prepare")
        self.request("owner-publish", token=prepared["token"])
        self.assertEqual(self.posts[0][:2], ("issues", "comments"))

    def test_uncertain_publication_readback_prevents_duplicate(self):
        self.request("owner-mutate", operation="reply", item="thread:T1", body="Acknowledged.")
        reply = self.view["replies"][-1]
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        prepared = self.request("owner-prepare")
        def ambiguous(*args, **kwargs):
            self.publish(*args, **kwargs)
            raise ApiError("network", "Lost response")
        with patch("pr_review.owner_publication.gh", side_effect=ambiguous):
            with self.assertRaises(ApiError):
                self.request("owner-publish", token=prepared["token"])
        view = self.request("owner-recover")
        self.assertEqual(view["replies"][-1]["status"], "published")
        self.assertEqual(len(self.posts), 1)

    def test_changed_discussion_preserves_human_wording_and_requires_inspection(self):
        self.request("owner-mutate", operation="reply", item="thread:T1", body="My precise wording")
        reply = self.view["replies"][-1]
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        self.feedback[0]["comments"][0]["body"] = "More feedback"
        view = self.request("owner-refresh")
        self.assertEqual(view["replies"][-1]["body"], "My precise wording")
        self.assertEqual(view["replies"][-1]["status"], "unstaged")
        with self.assertRaises(ApiError):
            self.request("owner-mutate", operation="stage", reply=reply["id"])
        self.request("owner-mutate", operation="acknowledge-reply", reply=reply["id"])
        self.request("owner-mutate", operation="stage", reply=reply["id"])

    def test_execution_lock_prevents_another_writer_but_allows_reads(self):
        session = OwnerSession({"session": self.path})
        with locked(session.execution_lock):
            self.request("owner-read")
            with self.assertRaises(ApiError) as caught:
                self.request("owner-mutate", operation="defer", items=["thread:T1"])
        self.assertEqual(caught.exception.code, "busy")

    def test_empty_change_request_is_visible_but_not_fixable(self):
        review = self.item("review:9", 9, "review")
        review["state"] = "CHANGES_REQUESTED"
        review["comments"][0]["body"] = ""
        self.feedback = [review]
        view = self.request("owner-refresh")
        item = next(i for i in view["feedback"] if i["key"] == "review:9")
        self.assertTrue(item["attention"])
        self.assertTrue(item["change_requested"])
        self.assertFalse(item["actionable"])

    def test_feedback_changes_between_tasks_pause_before_further_models(self):
        original = self.model
        def model(*args):
            result = original(*args)
            if args[3] == "fix":
                self.feedback[0]["comments"][0]["body"] = "New requirements arrived"
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model), self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "stale_discussion")
        state = self.request("owner-read")
        self.assertEqual(state["batches"][-1]["status"], "paused")
        self.assertTrue(Path(state["worktree"], "new.txt").exists())

    def test_partial_publication_retains_confirmed_reply_and_recovers_remaining(self):
        self.fix()
        self.commit_fix()
        self.request("owner-associate")
        prepared = self.request("owner-prepare")
        def partial(*args, **kwargs):
            if self.posts:
                raise ApiError("network", "Failed before second send")
            return self.publish(*args, **kwargs)
        with patch("pr_review.owner_publication.gh", side_effect=partial), self.assertRaises(ApiError):
            self.request("owner-publish", token=prepared["token"])
        view = self.request("owner-recover")
        self.assertEqual([r["status"] for r in view["replies"]], ["published", "unstaged"])
        self.request("owner-mutate", operation="stage", reply=view["replies"][1]["id"])
        prepared = self.request("owner-prepare")
        self.request("owner-publish", token=prepared["token"])
        self.assertEqual([p[1] for p in self.posts], ["comments/101/replies", "comments/102/replies"])

    def test_saved_confirmation_invalidated_by_draft_edit(self):
        self.request("owner-mutate", operation="reply", item="thread:T1", body="Original")
        reply = self.view["replies"][-1]
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        prepared = self.request("owner-prepare")
        self.request("owner-mutate", operation="edit", reply=reply["id"], body="Changed")
        with self.assertRaises(ApiError):
            self.request("owner-publish", token=prepared["token"])
        self.assertFalse(self.posts)

    def test_head_changes_during_publication_stop_before_the_next_send(self):
        self.fix()
        self.commit_fix()
        self.request("owner-associate")
        prepared = self.request("owner-prepare")
        def publish_then_change(*args, **kwargs):
            receipt = self.publish(*args, **kwargs)
            self.git("update-ref", "refs/pull/7/head", self.head)
            self.metadata["headRefOid"] = self.head
            return receipt
        with patch("pr_review.owner_publication.gh", side_effect=publish_then_change), self.assertRaises(ApiError) as caught:
            self.request("owner-publish", token=prepared["token"])
        self.assertEqual(caught.exception.code, "stale_confirmation")
        self.assertEqual(len(self.posts), 1)
        view = self.request("owner-refresh")
        self.assertEqual([r["status"] for r in view["replies"]], ["published", "unstaged"])

    def test_already_addressed_outcomes_never_generate_fixed_in_replies(self):
        original = self.model
        def model(*args):
            result = original(*args)
            if args[3] != "plan":
                for outcome in result["outcomes"]:
                    outcome["status"] = "already_addressed"
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            self.fix()
        self.commit_fix()
        view = self.request("owner-associate")
        self.assertEqual(view["replies"], [])

    def test_actual_provider_adapter_captures_edits_and_blocks_review_writes(self):
        self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        batch = state["batches"][-1]
        batch["settings"]["review"]["provider"] = "claude"
        result = {"summary": "Checked", "outcomes": [{"item": "thread:T1", "status": "fixed", "evidence": "Checked source"}],
                  "checks": ["focused check"], "complete": True, "blocking": []}
        def provider(*args, **kwargs):
            Path(kwargs["cwd"], "source.txt").write_text("unexpected review edit\n")
            return SimpleNamespace(returncode=0, events=[{"type": "result", "subtype": "success", "structured_output": result}])
        with patch("pr_review.owner_providers.run", side_effect=provider), self.assertRaises(ApiError) as caught:
            REAL_INVOKE(session, state, batch, "review", {"feedback": []})
        self.assertEqual(caught.exception.code, "integrity_failed")
        self.assertEqual(Path(state["worktree"], "source.txt").read_text(), "unexpected review edit\n")
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "integrity_failed")
        view = self.request("owner-fix", batch=batch["id"], adopt=True)
        self.assertEqual(view["batches"][-1]["status"], "ready")
        self.assertTrue(view["batches"][-1]["integrity_events"])

    def test_staged_changes_require_manual_index_reconciliation(self):
        self.fix()
        batch = self.view["batches"][-1]
        self.git("add", "source.txt", cwd=self.view["worktree"])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"], adopt=True)
        self.assertEqual(caught.exception.code, "staged_changes")
        self.assertTrue(self.git("diff", "--cached", cwd=self.view["worktree"]))
        self.assertEqual(Path(self.view["worktree"], "source.txt").read_text(), "fixed\n")

    def test_partial_diff_includes_untracked_changes_without_accepting_them(self):
        self.fix()
        worktree = self.view["worktree"]
        batch = self.view["batches"][-1]
        Path(worktree, "partial.txt").write_text("Retain partial fix\n")
        index = self.git("ls-files", "--stage", cwd=worktree)
        diff = self.request("owner-diff", batch=batch["id"], current=True)
        self.assertIn("Retain partial fix", diff["diff"])
        self.assertEqual(self.git("ls-files", "--stage", cwd=worktree), index)
        view = self.request("owner-read")
        self.assertEqual(view["batches"][-1]["checkpoint"], batch["checkpoint"])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "worktree_changed")

    def test_agent_configuration_trust_tracks_content_and_shows_exact_diff(self):
        self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        config = Path(state["worktree"], "AGENTS.md")
        config.write_text("New repository instructions\n")
        snapshot = owner_git.checkpoint(session, state, "config change")
        with self.assertRaises(ApiError) as caught:
            owner_git.trust(session, state, snapshot["tree"])
        self.assertEqual(caught.exception.code, "trust_required")
        self.assertIn("+New repository instructions", caught.exception.details["diff"])
        state["trusted_snapshot"] = caught.exception.details["trusted_snapshot"]
        Path(state["worktree"], "unrelated.txt").write_text("source change\n")
        owner_git.trust(session, state, owner_git.checkpoint(session, state, "source change")["tree"])
        config.write_text("Different instructions\n")
        with self.assertRaises(ApiError):
            owner_git.trust(session, state, owner_git.checkpoint(session, state, "new config")["tree"])

    def test_incomplete_fixer_verification_pauses_without_another_model(self):
        original = self.model
        def model(*args):
            result = original(*args)
            if args[3] == "fix":
                result["complete"] = False
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.fix()
        self.assertEqual(view["batches"][-1]["status"], "paused")
        self.assertEqual([role for role, _ in self.calls], ["plan", "fix"])
        self.assertEqual(Path(view["worktree"], "source.txt").read_text(), "fixed\n")

    def test_orphaned_attempt_is_recorded_and_not_blindly_replayed(self):
        self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        batch = state["batches"][-1]
        batch["attempts"].append({"role": "review", "status": "running"})
        session.save(state)
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "interrupted_task")
        view = self.request("owner-read")
        self.assertEqual(view["batches"][-1]["attempts"][-1]["status"], "interrupted")

    def test_fork_missing_and_ambiguous_remotes_are_explicit(self):
        def fork(*args, **kwargs):
            value = self.github(*args, **kwargs)
            if "head" in value:
                value["head"]["repo"]["html_url"] = "https://github.com/author/fork"
            return value
        with patch("pr_review.pulls.github_json", side_effect=fork):
            view = self.fix()
            self.assertEqual(view["push_target"], {"repository": "github.com/author/fork", "branch": "feature", "remotes": []})
            self.assertEqual(self.git("remote"), "origin")
            self.git("remote", "add", "author", "https://github.com/author/fork.git")
            self.git("remote", "add", "duplicate", "git@github.com:author/fork.git")
            view = self.request("owner-fix", batch=view["batches"][-1]["id"], adopt=True)
            self.assertEqual(view["push_target"]["remotes"], ["author", "duplicate"])
            self.assertNotIn("configured_target", view)

    def test_unique_fork_push_mapping_and_later_manual_config_are_preserved(self):
        self.git("remote", "add", "author", "https://github.com/author/fork.git")
        def fork(*args, **kwargs):
            value = self.github(*args, **kwargs)
            if "head" in value:
                value["head"]["repo"]["html_url"] = "https://github.com/author/fork"
            return value
        with patch("pr_review.pulls.github_json", side_effect=fork):
            view = self.fix()
            self.assertEqual(self.git("config", f"branch.{view['branch']}.merge"), "refs/heads/feature")
            self.assertEqual(self.git("config", f"branch.{view['branch']}.pushRemote"), "author")
            self.assertEqual(view["push_target"]["remotes"], ["author"])
            self.git("config", f"branch.{view['branch']}.merge", "refs/heads/manually-chosen")
            with self.assertRaises(ApiError) as caught:
                self.request("owner-fix", batch=view["batches"][-1]["id"])
            self.assertEqual(caught.exception.code, "integrity_failed")
            self.assertEqual(self.git("config", f"branch.{view['branch']}.merge"), "refs/heads/manually-chosen")

    def test_thread_resolution_is_explicit_and_uncertain_response_is_read_back(self):
        def resolve(*args, **kwargs):
            self.feedback[0]["is_resolved"] = True
            raise ApiError("network", "Response lost")
        with patch.object(Repository, "graphql", side_effect=resolve) as call:
            view = self.request("owner-thread-state", item="thread:T1", resolved=True)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(view["feedback"][0]["is_resolved"])
        self.assertNotIn("thread_intent", view)
        self.assertFalse(self.posts)

    def test_account_change_prevents_publication_without_losing_drafts(self):
        self.request("owner-mutate", operation="reply", item="thread:T1", body="Keep my wording")
        reply = self.view["replies"][-1]
        self.request("owner-mutate", operation="stage", reply=reply["id"])
        def other_user(*args, **kwargs):
            return {"login": "different-author"} if args[1] == "user" else self.github(*args, **kwargs)
        with patch("pr_review.pulls.github_json", side_effect=other_user), self.assertRaises(ApiError) as caught:
            self.request("owner-prepare")
        self.assertEqual(caught.exception.code, "account_changed")
        self.assertEqual(self.request("owner-read")["replies"][-1]["body"], "Keep my wording")
        self.assertFalse(self.posts)

    def test_task_dependencies_and_skip_do_not_run_dependents(self):
        third = self.item("thread:T3", 103)
        self.feedback.append(third)
        original = self.model
        def model(*args):
            if args[3] == "plan":
                return {"tasks": [
                    {"id": "base", "items": ["thread:T1"], "depends_on": [], "rationale": "Prerequisite", "expected": [{"item": "thread:T1", "outcome": "fixed"}]},
                    {"id": "caller", "items": ["thread:T2"], "depends_on": ["base"], "rationale": "Dependent", "expected": [{"item": "thread:T2", "outcome": "fixed"}]},
                    {"id": "other", "items": ["thread:T3"], "depends_on": [], "rationale": "Independent", "expected": [{"item": "thread:T3", "outcome": "fixed"}]}]}
            result = original(*args)
            if args[3] == "review" and args[4]["feedback"][0]["key"] == "thread:T1":
                result["complete"] = False
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            view = self.fix()
            batch = view["batches"][-1]
            self.assertEqual(batch["status"], "paused")
            self.calls.clear()
            view = self.request("owner-fix", batch=batch["id"], skip=["p1-base"])
        task_states = {t["id"]: t["status"] for t in view["batches"][-1]["tasks"]}
        self.assertEqual(task_states, {"p1-base": "skipped", "p1-caller": "blocked", "p1-other": "complete"})
        self.assertFalse(any(i["key"] == "thread:T2" for _, c in self.calls for i in c["feedback"]))


class PlanTest(unittest.TestCase):
    def task(self, identity, items, dependencies=()):
        return {"id": identity, "items": items, "depends_on": list(dependencies), "rationale": "Reason", "expected": [{"item": k, "outcome": "Result"} for k in items]}

    def test_order_combination_and_stable_tie_break(self):
        tasks = [self.task("caller", ["c"], ["interface"]), self.task("other", ["b"]), self.task("interface", ["a", "d"])]
        result = owner_fixes.validate_plan({"tasks": tasks}, [{"key": k} for k in "abcd"])
        self.assertEqual([t["id"] for t in result], ["interface", "other", "caller"])

    def test_cycle_duplicate_missing_and_added_items_rejected(self):
        plans = [
            [self.task("a", ["a"], ["b"]), self.task("b", ["b"], ["a"])],
            [self.task("a", ["a", "a", "b"])], [self.task("a", ["a"])],
            [self.task("a", ["a", "b", "c"])], [self.task("a", ["a", "b"], ["missing"])]]
        for tasks in plans:
            with self.subTest(tasks=tasks), self.assertRaises(ApiError):
                owner_fixes.validate_plan({"tasks": tasks}, [{"key": "a"}, {"key": "b"}])

    def test_plan_requires_per_item_expected_outcomes(self):
        task = self.task("combined", ["a", "b"])
        task["expected"] = [{"item": "a", "outcome": "Only the first item was considered"}]
        with self.assertRaises(ApiError):
            owner_fixes.validate_plan({"tasks": [task]}, [{"key": "a"}, {"key": "b"}])

    def test_timeline_receipts_validate_the_exact_pr_url_and_comment(self):
        session = SimpleNamespace(manifest={"repository": "github.com/org/repo", "pr": 7})
        action = {"kind": "timeline", "viewer": "author", "marker": "unique-marker"}
        value = {"id": 100, "user": {"login": "author"}, "body": "unique-marker", "html_url": None}
        self.assertFalse(owner_publication.valid_receipt(session, action, value))
        value["html_url"] = "https://github.com/org/repo/pull/8#issuecomment-100"
        self.assertFalse(owner_publication.valid_receipt(session, action, value))
        value["html_url"] = "https://github.com/Org/Repo/pull/7#issuecomment-100"
        self.assertTrue(owner_publication.valid_receipt(session, action, value))

    def test_provider_roles_have_distinct_write_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            schema = directory / "schema.json"
            schema.write_text(json.dumps(owner_providers.RESULT))
            for role in ("plan", "fix", "review"):
                choice = {"provider": "codex", "model": "chosen", "effort": "medium"}
                args = owner_providers.argv(choice, Runtime(), role, directory, schema)
                self.assertIn("sandbox_mode=" + ("workspace-write" if role == "fix" else "read-only"), args)
                self.assertIn("approval_policy=never", args)
                choice["provider"] = "claude"
                args = owner_providers.argv(choice, Runtime(), role, directory, schema)
                self.assertEqual("Edit" in args, role != "fix")
                self.assertIn("Bash(git commit:*)", args)
                self.assertIn("Bash(git add:*)", args)


if __name__ == "__main__":
    unittest.main()
