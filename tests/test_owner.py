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
from pr_review.common import ApiError, atomic_json, digest, locked
from pr_review.repository import Repository
from pr_review.owner_state import OwnerSession, feedback_body, superseded_batches
from pr_review import owner_commit, owner_completion, owner_fixes, owner_git, owner_providers, owner_publication
from pr_review.providers import Runtime
from pr_review.owner_cli import main as owner_cli
from pr_review.state import Session as ReviewSession
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

    def prepare_owner(self):
        session = OwnerSession({"session": self.path})
        state = session.load()
        owner_git.prepare(session, state)
        self.view = session.view(state)
        return Path(state["worktree"])

    def advance_pr(self, name="upstream.txt", content="new PR commit\n"):
        (self.repo / name).write_text(content)
        self.git("add", "--", name)
        self.git("commit", "-qm", "Advance PR")
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/7/head", sha)
        self.metadata["headRefOid"] = sha
        return sha

    def saved_review(self):
        path = self.root / "state/pr-review/github.com/org/repo/pr-7-review"
        atomic_json(path / "manifest.json", {
            "format_version": 2, "repository": "github.com/org/repo", "pr": 7, "url": self.metadata["url"],
            "git_common_dir": str(self.repo / ".git"), "worktree": str(self.repo), "run_id": "review", "status": "complete",
            "created_at": "2026-01-01T00:00:00Z", "viewer": "author",
            "snapshot": {"head": self.head, "base": self.base, "merge_base": self.base}})
        atomic_json(path / "reviews/synthesis.json", {"findings": [
            {"id": f"F{i}", "title": f"Local concern {i}", "body_md": f"Original finding {i}", "evidence": "Traced source.txt",
             "primary_location": {"path": "source.txt", "side": "RIGHT", "line": 1}, "locations": []}
            for i in (1, 2)]})
        atomic_json(path / "publication/contributions.json", {"proposals": [
            {"id": f"p{i}", "basis": {"finding_ids": [f"F{i}"], "body_md": f"Original finding {i}"},
             "body_md": f"Original finding {i}", "classification": "NEW", "destination": {"kind": "review"}}
            for i in (1, 2)]})
        return ReviewSession({"session": str(path)})

    def handoff(self, review, keys, **fields):
        return dispatch({"protocol": 1, "command": "owner-import", "session": str(review.root),
                         "generation": review.id, "revision": digest(review.load()), "comments": keys, **fields})

    def test_local_handoff_preserves_wording_staging_context_and_order_without_network(self):
        review = self.saved_review()
        state = review.load()
        state["comments"][0].update(body="Exact human wording", status="staged", human_edited=True)
        review.save(state)
        before = review.file.read_bytes()
        with patch.object(Repository, "metadata", side_effect=AssertionError("No network on import")):
            result = self.handoff(review, ["p2", "p1"])
        self.assertEqual(review.file.read_bytes(), before)
        self.assertFalse(self.calls)
        self.assertEqual([i["source"]["comment"] for i in result["owner"]["feedback"][:2]], ["p2", "p1"])
        item = result["owner"]["feedback"][1]
        self.assertEqual(item["comments"][0]["body"], "Exact human wording")
        self.assertEqual(item["source"]["findings"][0]["body_md"], "Original finding 1")
        self.assertEqual(item["source"]["snapshot"]["head"], self.head)
        self.assertTrue(item["review_context"]["rows"])
        self.assertTrue(item["actionable"])
        self.assertTrue(item["attention"])
        self.assertFalse(item["unread"])
        self.assertEqual(result["owner"]["session"], self.path)
        self.assertEqual(self.git("rev-parse", f"refs/pr-review/api/{review.id}/head"), self.head)

    def test_handoff_is_idempotent_and_draft_changes_are_explicitly_reimported(self):
        review = self.saved_review()
        first = self.handoff(review, ["p1"])
        key = first["items"][0]
        state = review.load()
        state["comments"][1]["body"] = "Unrelated edit"
        review.save(state)
        again = self.handoff(review, ["p1"])
        self.assertEqual(again["items"], [key])
        self.assertEqual(again["owner"]["feedback"][0]["versions"], [])
        self.request("owner-fix", items=[key])
        state = review.load()
        state["comments"][0]["body"] = "Changed requirement"
        review.save(state)
        unchanged = self.request("owner-refresh")
        local = next(i for i in unchanged["feedback"] if i["key"] == key)
        self.assertEqual(local["comments"][0]["body"], "Original finding 1")
        updated = self.handoff(review, ["p1"])["owner"]
        self.assertEqual(updated["feedback"][0]["comments"][0]["body"], "Changed requirement")
        self.assertEqual(updated["feedback"][0]["versions"][0]["comments"][0]["body"], "Original finding 1")
        self.assertTrue(updated["batches"][-1]["discussion_stale"])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=updated["batches"][-1]["id"])
        self.assertEqual(caught.exception.code, "stale_discussion")

    def test_handoff_validates_revision_and_all_ids_before_copying(self):
        review = self.saved_review()
        before = self.request("owner-read")
        for fields in ({"comments": ["missing"]}, {"comments": ["p1", "p1"]}, {"revision": "old"}):
            with self.subTest(fields=fields), self.assertRaises(ApiError):
                self.handoff(review, ["p1"], **fields)
        self.assertEqual(self.request("owner-read"), before)

    def test_handoff_busy_locks_do_not_alter_either_workflow(self):
        review = self.saved_review()
        before = self.request("owner-read")
        session = OwnerSession({"session": self.path})
        for lock in (session.execution_lock, review.pr_lock, review.lock):
            with self.subTest(lock=str(lock)), locked(lock), self.assertRaises(ApiError) as caught:
                self.handoff(review, ["p1"])
            self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self.request("owner-read"), before)

    def test_local_and_github_findings_share_fix_pipeline_but_only_github_gets_replies(self):
        review = self.saved_review()
        key = self.handoff(review, ["p1"])["items"][0]
        view = self.request("owner-fix", items=[key, "thread:T1"])
        self.assertEqual(view["batches"][-1]["status"], "ready")
        local = next(i for i in view["feedback"] if i["key"] == key)
        self.assertNotIn("missing", local)
        self.assertEqual(self.calls[0][1]["feedback"][0]["source"]["finding_ids"], ["F1"])
        sha = self.commit_fix()
        view = self.request("owner-associate")
        self.assertEqual(view["batches"][-1]["associations"][key]["commits"], [sha])
        self.assertEqual([r["source"] for r in view["replies"]], ["thread:T1"])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-mutate", operation="reply", item=key, body="No fabricated timeline reply")
        self.assertEqual(caught.exception.code, "local_only")
        with self.assertRaises(ApiError):
            self.request("owner-thread-state", item=key, resolved=True)
        self.assertFalse(self.posts)

    def test_local_commit_refresh_finishes_exact_local_fixes_and_preserves_other_work(self):
        keys = self.handoff(self.saved_review(), ["p1", "p2"])["items"]
        self.request("owner-fix", items=[keys[0], "thread:T1"])
        sha = self.commit_fix(push=False)
        worktree = Path(self.view["worktree"])
        (worktree / "manual.txt").write_text("unrelated staged work\n")
        self.git("add", "manual.txt", cwd=worktree)
        (worktree / "manual.txt").write_text("later unstaged edit\n")
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        before = (index.read_bytes(), self.git("show-ref"), len(self.calls))
        with patch.object(Repository, "metadata", side_effect=AssertionError("No network")), \
             patch("pr_review.owner_publication.associate", side_effect=AssertionError("No publication preparation")):
            view = self.request("owner-reconcile", revision=0)
        items = {i["key"]: i for i in view["feedback"]}
        self.assertEqual(view["batches"][-1]["commit_match"], {"commit": sha})
        self.assertEqual(view["reconciliation"]["handled_items"], [keys[0]])
        self.assertEqual(items[keys[0]]["triage"], "handled")
        self.assertEqual(items[keys[0]]["local_completion"]["commit"], sha)
        self.assertTrue(items[keys[1]]["attention"])
        self.assertTrue(items["thread:T1"]["attention"])
        self.assertFalse(view["replies"])
        self.assertFalse(self.posts)
        self.assertEqual((index.read_bytes(), self.git("show-ref"), len(self.calls)), before)
        self.assertEqual((worktree / "manual.txt").read_text(), "later unstaged edit\n")
        again = self.request("owner-reconcile")
        self.assertEqual(again["revision"], view["revision"])
        self.assertEqual(again["reconciliation"]["handled_items"], [])

    def test_commit_refresh_requires_current_complete_verified_feedback(self):
        key = self.handoff(self.saved_review(), ["p1"])["items"][0]
        view = self.request("owner-fix", items=[key])
        original = copy.deepcopy(view["batches"][-1])
        self.commit_fix(push=False)
        session = OwnerSession({"session": self.path})
        changes = [lambda b: b.update(status="paused"), lambda b: b.update(discussion_stale=True),
                   lambda b: b.update(integrity_failure="attempt"), lambda b: b.update(verified_items=[]),
                   lambda b: b["verified_fingerprints"].update({key: "older feedback"}),
                   lambda b: b["review"].update(complete=False),
                   lambda b: b["review"].update(blocking=["Not verified"]),
                   lambda b: b["review"]["outcomes"][0].update(status="already_addressed")]
        for change in changes:
            with self.subTest(change=change):
                state = session.load()
                state["batches"] = [copy.deepcopy(original)]
                change(state["batches"][0])
                session.save(state)
                view = self.request("owner-reconcile")
                self.assertEqual(view["reconciliation"]["handled_items"], [])
                self.assertEqual(next(i for i in view["feedback"] if i["key"] == key)["triage"], "attention")
        state = session.load()
        state["batches"] = [copy.deepcopy(original), {**copy.deepcopy(original), "id": "newer", "status": "paused"}]
        session.save(state)
        self.assertEqual(self.request("owner-reconcile")["reconciliation"]["handled_items"], [])

    def test_split_commits_do_not_automatically_finish_local_findings(self):
        key = self.handoff(self.saved_review(), ["p1"])["items"][0]
        self.request("owner-fix", items=[key])
        worktree = self.view["worktree"]
        self.git("add", "source.txt", cwd=worktree)
        self.git("commit", "-qm", "first part", cwd=worktree)
        self.git("add", "new.txt", cwd=worktree)
        self.git("commit", "-qm", "second part", cwd=worktree)
        view = self.request("owner-reconcile")
        self.assertNotIn("commit_match", view["batches"][-1])
        self.assertEqual(view["reconciliation"]["handled_items"], [])

    def test_explicit_restore_and_reimport_survive_automatic_commit_refresh(self):
        review = self.saved_review()
        key = self.handoff(review, ["p1"])["items"][0]
        self.request("owner-fix", items=[key])
        self.commit_fix(push=False)
        self.request("owner-reconcile")
        self.request("owner-mutate", operation="restore", items=[key])
        view = self.request("owner-reconcile")
        item = next(i for i in view["feedback"] if i["key"] == key)
        self.assertTrue(item["attention"])
        self.assertNotIn("local_completion", item)
        self.request("owner-mutate", operation="handled", items=[key])
        self.handoff(review, ["p1"])
        view = self.request("owner-reconcile")
        self.assertTrue(next(i for i in view["feedback"] if i["key"] == key)["attention"])

    def test_dropped_commit_reopens_only_automatically_finished_items(self):
        keys = self.handoff(self.saved_review(), ["p1", "p2"])["items"]
        view = self.request("owner-fix", items=keys)
        batch = view["batches"][-1]
        self.commit_fix(push=False)
        self.request("owner-mutate", operation="handled", items=[keys[1]])
        self.request("owner-reconcile")
        self.git("update-ref", "refs/heads/" + view["branch"], batch["baseline"])
        view = self.request("owner-reconcile")
        items = {i["key"]: i for i in view["feedback"]}
        self.assertEqual(view["reconciliation"]["reopened_items"], [keys[0]])
        self.assertNotIn("commit_match", view["batches"][-1])
        self.assertTrue(items[keys[0]]["attention"])
        self.assertEqual(items[keys[1]]["triage"], "handled")

    def test_exact_amendment_updates_completion_without_reopening_feedback(self):
        key = self.handoff(self.saved_review(), ["p1"])["items"][0]
        self.request("owner-fix", items=[key])
        self.commit_fix(push=False)
        self.request("owner-reconcile")
        self.git("commit", "--amend", "-qm", "amended message", cwd=self.view["worktree"])
        sha = self.git("rev-parse", "HEAD", cwd=self.view["worktree"])
        view = self.request("owner-reconcile")
        self.assertEqual(view["reconciliation"]["reopened_items"], [])
        self.assertEqual(view["reconciliation"]["handled_items"], [])
        self.assertEqual(next(i for i in view["feedback"] if i["key"] == key)["local_completion"]["commit"], sha)

    def test_commit_refresh_remains_readable_when_busy_or_worktree_is_missing(self):
        self.fix()
        session = OwnerSession({"session": self.path})
        before = session.file.read_bytes()
        with locked(session.execution_lock):
            view = self.request("owner-reconcile")
        self.assertEqual(view["reconciliation"]["status"], "busy")
        worktree = Path(view["worktree"])
        worktree.rename(worktree.with_name("moved-owner"))
        view = self.request("owner-reconcile")
        self.assertEqual(view["reconciliation"]["status"], "unavailable")
        self.assertEqual(session.file.read_bytes(), before)

    def test_commit_refresh_does_not_save_when_head_changes_during_check(self):
        self.fix()
        self.commit_fix(push=False)
        session = OwnerSession({"session": self.path})
        state = session.load()
        before = session.file.read_bytes()
        original = session.repo.git
        calls = 0
        def changed(*args, **kwargs):
            nonlocal calls
            if args == ("rev-parse", "HEAD"):
                calls += 1
                if calls == 2:
                    return SimpleNamespace(stdout="different-head\n")
            return original(*args, **kwargs)
        with patch.object(session.repo, "git", side_effect=changed):
            view = owner_completion.reconcile(session, state)
        self.assertEqual(view["reconciliation"]["status"], "unavailable")
        self.assertNotIn("commit_match", view["batches"][-1])
        self.assertEqual(session.file.read_bytes(), before)

    def test_cli_import_uses_the_saved_reviewer_and_preserves_selection(self):
        review = self.saved_review()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(owner_cli(["import", "--review-session", str(review.root), "--comment", "p2"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["owner"]["feedback"][0]["source"]["comment"], "p2")
        self.assertEqual(len(result["items"]), 1)

    def test_own_comments_can_be_fixed_without_unread_alerts(self):
        item = self.item("thread:SELF", 999)
        item["comments"][0]["author"] = "author"
        self.feedback = [item]
        view = self.request("owner-refresh")
        own = next(i for i in view["feedback"] if i["key"] == item["key"])
        self.assertTrue(own["actionable"])
        self.assertTrue(own["own_only"])
        self.assertEqual(own["triage"], "available")
        self.assertFalse(own["unread"])
        self.assertFalse(own["attention"])
        item["comments"][0]["body"] = "Updated own feedback"
        own = self.request("owner-refresh")["feedback"][0]
        self.assertFalse(own["unread"])
        self.assertFalse(own["attention"])
        view = self.request("owner-fix", items=[item["key"]])
        self.assertEqual(view["batches"][-1]["status"], "ready")
        self.commit_fix()
        view = self.request("owner-associate")
        self.assertEqual(view["replies"][0]["target"]["root_id"], 999)
        preview = self.request("owner-prepare")
        view = self.request("owner-publish", token=preview["token"])
        self.assertEqual(view["replies"][0]["status"], "published")
        self.assertEqual(self.posts[0][1], "comments/999/replies")

    def test_receipt_only_review_is_history_and_preserves_raw_body(self):
        marker = f"<!-- pr-review generation=abc head={self.head} action=api-123 -->"
        review = self.item("review:9", 9, "review")
        review.update(state="COMMENTED")
        review["comments"][0].update(author="author", body="\n\n" + marker)
        self.feedback = [review]
        # Simulate the state saved before receipt markers were recognized.
        session = OwnerSession({"session": self.path})
        state = session.load()
        state["feedback"] = [{**copy.deepcopy(review), "actionable": True,
                              "own_only": True, "triage": "available", "queued": True}]
        session.save(state)
        item = self.request("owner-refresh")["feedback"][0]
        self.assertFalse(item["actionable"])
        self.assertFalse(item["own_only"])
        self.assertFalse(item["attention"])
        self.assertEqual(item["triage"], "handled")
        self.assertEqual(item["comments"][0]["display_body"], "")
        self.assertEqual(item["comments"][0]["body"], "\n\n" + marker)
        self.assertEqual(item["fingerprint"], digest([review["key"], review["comments"]]))
        self.assertNotIn("display_body", session.load()["feedback"][0]["comments"][0])
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", items=[review["key"]])
        self.assertEqual(caught.exception.code, "invalid_target")

    def test_receipt_display_preserves_examples_and_other_html_comments(self):
        marker = f"<!-- pr-review generation=abc head={self.head} action=api-123 -->"
        for body in ("<!-- an ordinary HTML comment -->", "```html\n" + marker + "\n```",
                     marker + "\nPlease investigate this marker."):
            with self.subTest(body=body):
                self.assertEqual(feedback_body(body), body)
        self.assertEqual(feedback_body("Actual feedback\n\n" + marker + "\n"), "Actual feedback")
        self.assertEqual(feedback_body("<!-- pr-owner session=" + "a" * 24 + " action=123 -->"), "")

    def test_receipts_do_not_hide_real_feedback_or_change_request(self):
        marker = f"<!-- pr-review generation=abc head={self.head} action=api-123 -->"
        review = self.item("review:9", 9, "review")
        review.update(state="CHANGES_REQUESTED")
        review["comments"][0]["body"] = marker
        self.feedback[0]["comments"][0]["body"] = "Please fix this\n\n" + marker
        self.feedback.append(review)
        items = {i["key"]: i for i in self.request("owner-refresh")["feedback"]}
        self.assertTrue(items["thread:T1"]["actionable"])
        self.assertEqual(items["thread:T1"]["comments"][0]["display_body"], "Please fix this")
        self.assertTrue(items["review:9"]["change_requested"])
        self.assertTrue(items["review:9"]["attention"])
        self.assertFalse(items["review:9"]["actionable"])
        self.assertEqual(items["review:9"]["comments"][0]["display_body"], "")

    def test_failed_preflight_can_resume_with_corrected_settings(self):
        with patch("pr_review.owner_providers.preflight", side_effect=ApiError("unsupported_model", "Unsupported")):
            with self.assertRaises(ApiError):
                self.fix()
        batch = self.request("owner-read")["batches"][-1]
        self.assertEqual(batch["attempts"], [])
        self.assertEqual(batch["tasks"], [])
        settings = copy.deepcopy(self.view["settings"])
        settings["fix"]["model"] = "supported-fix"
        settings["review"]["model"] = "supported-review"
        self.request("owner-mutate", operation="settings", settings=settings)
        view = self.request("owner-fix", batch=batch["id"])
        self.assertEqual(len(view["batches"]), 1)
        resumed = view["batches"][0]
        self.assertEqual(resumed["id"], batch["id"])
        self.assertEqual(resumed["settings"], settings)
        self.assertEqual(resumed["baseline"], batch["baseline"])
        self.assertEqual(resumed["items"], batch["items"])
        self.assertEqual(resumed["status"], "ready")

    def unused_batch_then_verified_replacement(self):
        with patch("pr_review.owner_providers.preflight", side_effect=ApiError("unsupported_model", "Unsupported")), self.assertRaises(ApiError):
            self.fix()
        return self.fix()

    def test_unused_duplicate_is_superseded_without_rewriting_history(self):
        view = self.unused_batch_then_verified_replacement()
        old, replacement = view["batches"]
        self.assertEqual(old["superseded_by"], [replacement["id"]])
        self.assertEqual(old["status"], "paused")
        self.assertEqual(old["error"], "Unsupported")
        session = OwnerSession({"session": self.path})
        saved = copy.deepcopy(session.load()["batches"][0])
        self.assertNotIn("superseded_by", saved)
        with patch("pr_review.owner_providers.preflight") as preflight, self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=old["id"])
        self.assertEqual(caught.exception.code, "superseded_batch")
        self.assertEqual(caught.exception.details["superseded_by"], [replacement["id"]])
        preflight.assert_not_called()
        self.assertEqual(session.load()["batches"][0], saved)

    def test_supersession_requires_untouched_batch_and_current_complete_verification(self):
        view = self.unused_batch_then_verified_replacement()
        old_id = view["batches"][0]["id"]
        changes = [
            lambda s: s["batches"][0].update(attempts=[{"role": "plan", "status": "failed"}]),
            lambda s: s["batches"][0].update(tasks=[{"id": "work"}]),
            lambda s: s["batches"][0]["checkpoint"].update(label="resume"),
            lambda s: s["batches"][0].update(integrity_failure="Inspect source"),
            lambda s: s["batches"][1].update(status="paused"),
            lambda s: s["batches"][1].update(verified_items=["thread:T1"]),
            lambda s: s["batches"][1]["review"].update(complete=False),
            lambda s: s["batches"][1].update(discussion_stale=True),
            lambda s: s["feedback"][0].update(fingerprint="changed"),
            lambda s: s["feedback"][0].update(missing=True),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                state = copy.deepcopy(view)
                change(state)
                self.assertNotIn(old_id, superseded_batches(state))

    def test_separate_replacement_batches_must_cover_every_original_item(self):
        with patch("pr_review.owner_providers.preflight", side_effect=ApiError("unsupported_model", "Unsupported")), self.assertRaises(ApiError):
            self.fix()
        view = self.request("owner-fix", items=["thread:T1"])
        self.assertNotIn("superseded_by", view["batches"][0])
        self.commit_fix()
        view = self.request("owner-fix", items=["thread:T2"])
        self.assertEqual(view["batches"][0]["superseded_by"], [b["id"] for b in view["batches"][1:]])

    def test_superseded_unused_batch_does_not_block_clean_fast_forward(self):
        with patch("pr_review.owner_providers.preflight", side_effect=ApiError("unsupported_model", "Unsupported")), self.assertRaises(ApiError):
            self.fix()
        view = self.fix(verify_only=True)
        self.assertIn("superseded_by", view["batches"][0])
        head = self.advance_pr()
        view = self.fix(verify_only=True)
        self.assertEqual(view["batches"][-1]["baseline"], head)
        self.assertEqual(view["batches"][-1]["status"], "ready")

    def test_invalid_plan_is_corrected_before_any_fix_runs(self):
        plans = []
        def model(session, state, batch, role, context):
            result = self.model(session, state, batch, role, context)
            if role == "plan":
                plans.append(copy.deepcopy(context))
                if len(plans) < 3:
                    result["tasks"][0]["depends_on"] = ["thread:T1"]
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=model):
            batch = self.fix()["batches"][-1]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual([r for r, _ in self.calls], ["plan", "plan", "plan", "fix", "review", "review"])
        self.assertEqual(len(batch["plan_rejections"]), 2)
        self.assertIn("shared -> thread:T1", plans[1]["plan_correction"]["error"])
        self.assertEqual(plans[1]["plan_correction"]["plan"]["tasks"][0]["depends_on"], ["thread:T1"])

    def test_invalid_plan_retries_are_bounded_and_resume_gets_saved_error(self):
        def invalid(session, state, batch, role, context):
            self.assertEqual(role, "plan")
            result = self.model(session, state, batch, role, context)
            result["tasks"][0]["depends_on"] = ["thread:T1"]
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=invalid), self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "invalid_plan")
        batch = self.request("owner-read")["batches"][-1]
        self.assertEqual(len(batch["plan_rejections"]), 3)
        self.assertEqual(batch["status"], "paused")
        self.assertEqual(batch["tasks"], [])
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.git("status", "--porcelain", cwd=self.view["worktree"]))
        self.request("owner-fix", batch=batch["id"])
        self.assertIn("plan_correction", self.calls[3][1])
        self.assertEqual(self.view["batches"][-1]["status"], "ready")

    def test_old_rejected_plan_is_explained_when_resuming(self):
        with patch("pr_review.owner_providers.preflight", side_effect=ApiError("unsupported_model", "Unsupported")), self.assertRaises(ApiError):
            self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        batch = state["batches"][-1]
        plan = self.model(session, state, batch, "plan", {"feedback": state["feedback"]})
        plan["tasks"][0]["depends_on"] = ["thread:T1"]
        batch["attempts"].append({"role": "plan", "status": "complete", "result": plan})
        session.save(state)
        self.calls.clear()
        self.request("owner-fix", batch=batch["id"])
        self.assertEqual(self.calls[0][0], "plan")
        self.assertIn("shared -> thread:T1", self.calls[0][1]["plan_correction"]["error"])

    def test_plan_correction_stops_if_feedback_changes(self):
        def invalid(*args):
            result = self.model(*args)
            result["tasks"][0]["depends_on"] = ["missing"]
            self.feedback[0]["comments"][0]["body"] = "New requirements"
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=invalid), self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "stale_discussion")
        self.assertEqual([r for r, _ in self.calls], ["plan"])

    def test_provider_failure_during_planning_is_not_retried(self):
        with patch("pr_review.owner_providers.invoke", side_effect=ApiError("model_failed", "Provider failed")) as invoke:
            with self.assertRaises(ApiError) as caught:
                self.fix()
        self.assertEqual(caught.exception.code, "model_failed")
        self.assertEqual(invoke.call_count, 1)

    def test_plan_correction_stops_if_pr_head_changes(self):
        def invalid(*args):
            result = self.model(*args)
            result["tasks"][0]["depends_on"] = ["missing"]
            self.metadata["headRefOid"] = "f" * 40
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=invalid), self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "stale_source")
        self.assertEqual([r for r, _ in self.calls], ["plan"])

    def test_started_planning_keeps_saved_provider_settings(self):
        def failed_plan(session, state, batch, role, context):
            batch["attempts"].append({"role": role, "status": "failed"})
            session.save(state)
            raise ApiError("model_failed", "Planner failed")
        with patch("pr_review.owner_providers.invoke", side_effect=failed_plan), self.assertRaises(ApiError):
            self.fix()
        batch = self.request("owner-read")["batches"][-1]
        settings = copy.deepcopy(self.view["settings"])
        settings["fix"]["model"] = "different"
        self.request("owner-mutate", operation="settings", settings=settings)
        with patch("pr_review.owner_providers.invoke") as invoke, self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "settings_changed")
        invoke.assert_not_called()
        self.assertEqual(self.request("owner-read")["batches"][-1]["settings"], batch["settings"])
        self.request("owner-mutate", operation="settings", settings=batch["settings"])
        self.assertEqual(self.request("owner-fix", batch=batch["id"])["batches"][-1]["status"], "ready")

    def test_own_general_comment_can_be_queued_without_an_unread_alert(self):
        item = self.item("comment:999", 999, "comment")
        item["comments"][0]["author"] = "author"
        self.feedback = [item]
        self.request("owner-refresh")
        self.request("owner-mutate", operation="restore", items=[item["key"]])
        own = self.request("owner-refresh")["feedback"][0]
        self.assertTrue(own["attention"])
        self.assertFalse(own["unread"])
        self.request("owner-mutate", operation="handled", items=[item["key"]])
        self.assertFalse(self.request("owner-refresh")["feedback"][0]["attention"])

    def test_incoming_reply_to_own_thread_creates_attention(self):
        item = self.item("thread:SELF", 999)
        item["comments"][0]["author"] = "author"
        self.feedback = [item]
        self.request("owner-refresh")
        self.request("owner-mutate", operation="handled", items=[item["key"]])
        item["comments"].append({**item["comments"][0], "id": 1000, "author": "teammate", "body": "Additional concern"})
        own = self.request("owner-refresh")["feedback"][0]
        self.assertFalse(own["own_only"])
        self.assertTrue(own["attention"])
        self.assertTrue(own["unread"])

    def test_legacy_own_comment_becomes_available_without_generating_an_alert(self):
        item = self.item("thread:SELF", 999)
        item["comments"][0]["author"] = "author"
        self.feedback = [item]
        self.request("owner-refresh")
        session = OwnerSession({"session": self.path})
        state = session.load()
        own = state["feedback"][0]
        own.pop("own_only")
        own.update(actionable=False, triage="handled")
        session.save(state)
        own = self.request("owner-refresh")["feedback"][0]
        self.assertEqual(own["triage"], "available")
        self.assertTrue(own["actionable"])
        self.assertFalse(own["unread"])

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

    def test_new_fix_fast_forwards_after_previous_fix_and_pr_advance(self):
        self.request("owner-fix", items=["thread:T1"])
        committed = self.commit_fix()
        previous = copy.deepcopy(self.request("owner-associate")["batches"][0])
        self.git("merge", "--ff-only", committed)
        remote = self.advance_pr()
        self.calls.clear()
        with patch.object(owner_git, "emit") as progress:
            view = self.request("owner-fix", items=["thread:T2"])
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["baseline"], remote)
        self.assertEqual(batch["remote_head"], remote)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), remote)
        self.assertEqual(self.git("rev-parse", batch["baseline_ref"]), remote)
        self.assertEqual(Path(view["worktree"], "upstream.txt").read_text(), "new PR commit\n")
        self.assertEqual(self.calls[0][1]["baseline"], remote)
        self.assertEqual(view["batches"][0], previous)
        self.assertEqual(self.git("rev-parse", "HEAD"), remote)
        messages = [call.kwargs["message"] for call in progress.call_args_list]
        self.assertTrue(any("fast-forwarding" in message for message in messages))
        self.assertTrue(any("updated to " + remote[:12] in message for message in messages))

    def test_new_fix_retains_unpushed_owner_commits(self):
        self.fix()
        local = self.commit_fix(push=False)
        view = self.fix(verify_only=True)
        self.assertEqual(view["batches"][-1]["baseline"], local)
        self.assertEqual(view["batches"][-1]["remote_head"], self.head)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), local)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_behind_owner_worktree_preserves_unstaged_staged_and_untracked_changes(self):
        worktree = self.prepare_owner()
        remote = self.advance_pr()
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        for kind in ("unstaged", "staged", "untracked"):
            with self.subTest(kind=kind):
                path = worktree / ("extra.txt" if kind == "untracked" else "source.txt")
                path.write_text("keep my work\n")
                if kind == "staged":
                    self.git("add", "source.txt", cwd=worktree)
                before = index.read_bytes()
                with self.assertRaises(ApiError) as caught:
                    self.fix()
                self.assertEqual(caught.exception.code, "dirty_worktree")
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)
                self.assertEqual(path.read_text(), "keep my work\n")
                self.assertEqual(index.read_bytes(), before)
                self.assertFalse(self.calls)
                self.assertEqual(self.request("owner-read")["batches"], [])
                if kind == "untracked":
                    path.unlink()
                else:
                    self.git("restore", "--staged", "--worktree", "source.txt", cwd=worktree)
        self.assertEqual(self.git("rev-parse", "refs/pull/7/head"), remote)

    def test_genuine_owner_divergence_still_requires_manual_reconciliation(self):
        worktree = self.prepare_owner()
        (worktree / "local.txt").write_text("local commit\n")
        self.git("add", "local.txt", cwd=worktree)
        self.git("commit", "-qm", "Local work", cwd=worktree)
        local = self.git("rev-parse", "HEAD", cwd=worktree)
        remote = self.advance_pr()
        with self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "diverged")
        self.assertIn("C-u m", str(caught.exception))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), local)
        self.assertEqual(self.git("rev-parse", "refs/pull/7/head"), remote)
        self.assertFalse((worktree / "upstream.txt").exists())
        self.assertFalse(self.calls)

    def test_resume_never_fast_forwards_an_existing_batch(self):
        view = self.fix(verify_only=True)
        batch = copy.deepcopy(view["batches"][-1])
        self.advance_pr()
        self.calls.clear()
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", batch=batch["id"])
        self.assertEqual(caught.exception.code, "stale_source")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), self.head)
        self.assertEqual(self.request("owner-read")["batches"][-1], batch)
        self.assertFalse(self.calls)

    def test_new_fix_does_not_move_an_unfinished_batches_clean_baseline(self):
        original = self.model
        def model(*args):
            value = original(*args)
            if args[3] == "review":
                value["blocking"] = ["Needs further work"]
            return value
        with patch.object(owner_providers, "invoke", side_effect=model):
            view = self.fix(verify_only=True)
        batch = copy.deepcopy(view["batches"][-1])
        self.advance_pr()
        self.calls.clear()
        with self.assertRaises(ApiError) as caught:
            self.fix()
        self.assertEqual(caught.exception.code, "worktree_behind")
        self.assertIn("unfinished batch", str(caught.exception))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), self.head)
        self.assertEqual(self.request("owner-read")["batches"][-1], batch)
        self.assertFalse(self.calls)

    def test_fast_forward_preserves_ignored_files_and_disables_merge_overrides(self):
        worktree = self.prepare_owner()
        self.git("config", "merge.autostash", "true")
        self.git("config", f"branch.{self.view['branch']}.mergeOptions", "--squash")
        (self.repo / ".git/info/exclude").write_text("ignored.txt\n")
        (worktree / "ignored.txt").write_text("keep ignored data\n")
        remote = self.advance_pr()
        view = self.fix(verify_only=True)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), remote)
        self.assertEqual((worktree / "ignored.txt").read_text(), "keep ignored data\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=worktree), "")
        self.assertEqual(self.git("stash", "list"), "")
        self.assertEqual(view["batches"][-1]["baseline"], remote)

    def test_fast_forward_refuses_to_overwrite_ignored_files(self):
        worktree = self.prepare_owner()
        (self.repo / ".git/info/exclude").write_text("collision.txt\n")
        (worktree / "collision.txt").write_text("keep ignored data\n")
        (self.repo / "collision.txt").write_text("incoming tracked data\n")
        self.git("add", "-f", "collision.txt")
        self.git("commit", "-qm", "Track formerly ignored file")
        self.metadata["headRefOid"] = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/7/head", self.metadata["headRefOid"])
        with self.assertRaises(ApiError):
            self.fix()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)
        self.assertEqual((worktree / "collision.txt").read_text(), "keep ignored data\n")
        self.assertFalse(self.calls)
        self.assertEqual(self.request("owner-read")["batches"], [])

    def test_new_fix_refuses_in_progress_git_and_invalid_feedback_before_sync(self):
        worktree = self.prepare_owner()
        self.advance_pr()
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            with self.subTest(name=name):
                marker = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", name, cwd=worktree))
                if name in ("rebase-merge", "rebase-apply", "sequencer"):
                    marker.mkdir()
                else:
                    marker.write_text(self.base + "\n")
                with self.assertRaises(ApiError) as caught:
                    self.fix()
                self.assertEqual(caught.exception.code, "git_in_progress")
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)
                marker.rmdir() if marker.is_dir() else marker.unlink()
        with self.assertRaises(ApiError) as caught:
            self.request("owner-fix", items=["missing"])
        self.assertEqual(caught.exception.code, "invalid_target")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)
        self.assertFalse(self.calls)

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

    def publish_fix_with_thread_readback(self):
        for item in self.feedback:
            item["comments"][0]["author"] = "author"
        self.request("owner-refresh")
        self.fix()
        self.commit_fix()
        self.request("owner-associate")
        preview = self.request("owner-prepare")
        view = self.request("owner-publish", token=preview["token"])
        for reply in view["replies"]:
            receipt = reply["receipt"]
            item = next(i for i in self.feedback if i["key"] == reply["source"])
            item["comments"].append({"id": receipt["id"], "author": "author", "body": receipt["body"],
                                     "url": receipt["html_url"], "created_at": "today", "updated_at": "today"})
        return view

    def test_confirmed_own_replies_do_not_stale_fix_and_resolved_items_finish(self):
        view = self.publish_fix_with_thread_readback()
        original = view["batches"][-1]["verified_fingerprints"]
        # Simulate a refresh performed by the previous version.
        session = OwnerSession({"session": self.path})
        state = session.load()
        state["batches"][-1]["discussion_stale"] = True
        for item in state["feedback"]:
            raw = next(i for i in self.feedback if i["key"] == item["key"])
            item["comments"] = copy.deepcopy(raw["comments"])
            item["fingerprint"] = digest([item["key"], item["comments"]])
        session.save(state)
        for item in self.feedback:
            item["is_resolved"] = True
        view = self.request("owner-refresh")
        self.assertFalse(view["batches"][-1]["discussion_stale"])
        for item in view["feedback"]:
            self.assertEqual(item["fingerprint"], original[item["key"]])
            self.assertEqual(len(item["comments"]), 2)  # Conversation text is retained.
            self.assertEqual(item["triage"], "handled")
            self.assertFalse(item["attention"])
            self.assertIn("github_completion", item)

    def test_deleted_completed_thread_stays_missing_without_attention(self):
        self.publish_fix_with_thread_readback()
        self.feedback[0]["is_resolved"] = True
        view = self.request("owner-refresh")
        self.assertIn("github_completion", view["feedback"][0])
        deleted = self.feedback.pop(0)
        for _ in range(2):
            view = self.request("owner-refresh")
            item = next(i for i in view["feedback"] if i["key"] == deleted["key"])
            self.assertTrue(item["missing"])
            self.assertFalse(item["attention"])
            self.assertNotIn("github_completion", item)
        deleted["is_resolved"] = False
        self.feedback.append(deleted)
        view = self.request("owner-refresh")
        item = next(i for i in view["feedback"] if i["key"] == deleted["key"])
        self.assertFalse(item.get("missing"))
        self.assertTrue(item["attention"])

    def test_resolved_completion_reopens_for_new_feedback_and_respects_manual_restore(self):
        self.publish_fix_with_thread_readback()
        self.feedback[0]["is_resolved"] = True
        self.request("owner-refresh")
        self.request("owner-mutate", operation="restore", items=["thread:T1"])
        view = self.request("owner-refresh")
        self.assertTrue(view["feedback"][0]["attention"])
        self.assertEqual(view["feedback"][0]["triage"], "attention")
        # A different automatically finished item must reopen for a new concern.
        self.feedback[1]["is_resolved"] = True
        self.request("owner-refresh")
        self.feedback[1]["comments"].append({"id": 9999, "author": "reviewer", "body": "This still fails",
                                           "url": "https://example.com/new", "created_at": "later", "updated_at": "later"})
        view = self.request("owner-refresh")
        self.assertTrue(view["feedback"][1]["attention"])
        self.assertNotIn("github_completion", view["feedback"][1])
        self.assertTrue(view["batches"][-1]["discussion_stale"])

    def test_edited_own_receipt_is_a_real_feedback_change(self):
        self.publish_fix_with_thread_readback()
        self.feedback[0]["is_resolved"] = True
        self.request("owner-refresh")
        self.feedback[0]["comments"][-1]["body"] = "Actually, this needs more work"
        view = self.request("owner-refresh")
        self.assertTrue(view["batches"][-1]["discussion_stale"])
        self.assertTrue(view["feedback"][0]["attention"])
        self.assertNotIn("github_completion", view["feedback"][0])

    def test_reopened_thread_returns_completed_fix_to_attention(self):
        self.publish_fix_with_thread_readback()
        self.feedback[0]["is_resolved"] = True
        view = self.request("owner-refresh")
        self.assertEqual(view["feedback"][0]["triage"], "handled")
        self.feedback[0]["is_resolved"] = False
        view = self.request("owner-refresh")
        self.assertTrue(view["feedback"][0]["attention"])
        self.assertEqual(view["feedback"][0]["triage"], "attention")
        self.assertNotIn("github_completion", view["feedback"][0])
        self.assertFalse(view["batches"][-1]["discussion_stale"])

    def test_unrecognized_own_reply_is_not_filtered(self):
        self.fix()
        self.feedback[0]["comments"].append({"id": 9999, "author": "author", "body": "More investigation needed",
                                           "url": "https://example.com/new", "created_at": "later", "updated_at": "later"})
        view = self.request("owner-refresh")
        self.assertTrue(view["batches"][-1]["discussion_stale"])

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
        owner_git.trust(session, state, snapshot["tree"], approval=caught.exception.details["trusted_snapshot"])
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
        self.request("owner-fix", batch=view["batches"][-1]["id"])
        self.assertEqual([role for role, _ in self.calls], ["plan", "fix", "fix", "review", "review"])

    def test_resume_incomplete_review_preserves_fix_and_retries_verification(self):
        original = self.model
        def incomplete(*args):
            result = original(*args)
            if args[3] == "review":
                result.update(complete=False, blocking=["Real-Git fixture could not run"])
            return result
        with patch("pr_review.owner_providers.invoke", side_effect=incomplete):
            view = self.fix()
        batch = view["batches"][-1]
        self.assertEqual(batch["status"], "paused")
        self.assertEqual([role for role, _ in self.calls], ["plan", "fix", "review"])
        before = batch["tasks"][0]["before"]
        view = self.request("owner-fix", batch=batch["id"])
        self.assertEqual(view["batches"][-1]["status"], "ready")
        self.assertEqual([role for role, _ in self.calls], ["plan", "fix", "review", "review", "review"])
        retry = self.calls[-2][1]
        self.assertIn("Real-Git fixture could not run", retry["previous_incomplete_review"]["blocking"])
        self.assertIn("+fixed", retry["task_diff"])
        self.assertEqual(view["batches"][-1]["tasks"][0]["before"], before)
        prepared = self.request("owner-prepare-commit", batch=batch["id"])
        self.assertTrue(prepared["ready"])
        self.assertEqual(self.git("write-tree", cwd=view["worktree"]), view["batches"][-1]["final_tree"])

    def test_provider_review_runs_real_git_fixtures_without_touching_owner_index(self):
        self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        batch = state["batches"][-1]
        batch["settings"]["review"]["provider"] = "codex"
        worktree = Path(state["worktree"])
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        before = index.read_bytes()
        tree = batch["checkpoint"]["tree"]
        result = {"summary": "Real Git checked", "outcomes": [], "checks": ["scratch Git fixture"],
                  "complete": True, "blocking": []}
        def provider(args, **kwargs):
            scratch = Path(kwargs["env"]["TMPDIR"])
            self.assertEqual(args[args.index("--cd") + 1], str(scratch))
            self.assertFalse(scratch.is_relative_to(worktree))
            self.assertEqual(kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
            prompt = kwargs["input_bytes"].decode()
            self.assertIn(str(worktree), prompt)
            self.assertIn("uncommitted and untracked fixes", prompt)
            self.assertIn("only in those disposable repositories", prompt)
            with tempfile.TemporaryDirectory(dir=scratch) as fixture:
                self.git("init", "-q", cwd=fixture)
                Path(fixture, "test.txt").write_text("fixture\n")
                self.git("add", ".", cwd=fixture)
                self.git("commit", "-qm", "scratch only", cwd=fixture)
                self.assertEqual(self.git("show", "HEAD:test.txt", cwd=fixture), "fixture")
            Path(args[args.index("-o") + 1]).write_text(json.dumps(result))
            return SimpleNamespace(returncode=0, events=[{"type": "turn.completed"}])
        with patch("pr_review.owner_providers.run", side_effect=provider):
            self.assertEqual(REAL_INVOKE(session, state, batch, "review", {"feedback": []}), result)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(batch["checkpoint"]["tree"], tree)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), batch["baseline"])

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
            self.assertEqual(self.git("config", f"branch.{view['branch']}.remote"), "author")
            self.assertNotIn("pushremote", self.git("config", "--local", "--list").lower())
            self.assertEqual(view["push_target"]["remotes"], ["author"])
            self.git("config", f"branch.{view['branch']}.merge", "refs/heads/manually-chosen")
            with self.assertRaises(ApiError) as caught:
                self.request("owner-fix", batch=view["batches"][-1]["id"])
            self.assertEqual(caught.exception.code, "integrity_failed")
            self.assertEqual(self.git("config", f"branch.{view['branch']}.merge"), "refs/heads/manually-chosen")

    def test_commit_preparation_stages_verified_blobs_and_preserves_other_work(self):
        view = self.fix()
        worktree = Path(view["worktree"])
        (worktree / "source.txt").write_text("fixed\nmy later edit\n")
        (worktree / "unrelated.txt").write_text("do not stage\n")
        calls = len(self.calls)
        result = self.request("owner-prepare-commit")
        self.assertTrue(result["ready"])
        self.assertEqual(result["paths"], ["new.txt", "source.txt"])
        self.assertEqual(self.git("write-tree", cwd=worktree), view["batches"][-1]["final_tree"])
        self.assertEqual(self.git("show", ":source.txt", cwd=worktree), "fixed")
        self.assertIn("my later edit", (worktree / "source.txt").read_text())
        self.assertIn("unrelated.txt", self.git("ls-files", "--others", "--exclude-standard", cwd=worktree))
        self.assertIn("my later edit", self.git("diff", cwd=worktree))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)
        self.assertIn("Verified fix", result["message"])
        self.assertIn(self.metadata["url"], result["message"])
        self.assertEqual(len(self.calls), calls)
        self.assertFalse(self.posts)

    def test_commit_preparation_accepts_exact_staging_and_can_be_repeated(self):
        view = self.fix()
        self.git("add", "source.txt", cwd=view["worktree"])
        for _ in range(2):
            result = self.request("owner-prepare-commit", batch=view["batches"][-1]["id"])
            self.assertTrue(result["ready"])
            self.assertEqual(self.git("write-tree", cwd=view["worktree"]), view["batches"][-1]["final_tree"])

    def test_consecutive_fixes_prepare_commits_at_the_updated_owner_head(self):
        view = self.request("owner-fix", items=["thread:T1"])
        first = self.request("owner-prepare-commit", batch=view["batches"][-1]["id"])
        self.assertTrue(first["ready"])
        self.git("commit", "-qm", first["message"], cwd=view["worktree"])
        committed = self.git("rev-parse", "HEAD", cwd=view["worktree"])
        self.request("owner-mutate", operation="handled", items=["thread:T1"])
        original = self.model
        def model(session, state, batch, role, context):
            result = original(session, state, batch, role, context)
            if role == "fix":
                Path(state["worktree"], "source.txt").write_text("second fix\n")
                batch["checkpoint"] = owner_git.checkpoint(session, state, "second fix")
            return result
        with patch.object(owner_providers, "invoke", side_effect=model):
            view = self.request("owner-fix", items=["thread:T2"])
        batch = view["batches"][-1]
        self.assertEqual(batch["baseline"], committed)
        second = self.request("owner-prepare-commit", batch=batch["id"])
        self.assertTrue(second["ready"])
        self.assertEqual(self.git("write-tree", cwd=view["worktree"]), batch["final_tree"])
        self.assertIn("second fix", self.git("diff", "--cached", cwd=view["worktree"]))
        self.assertIn("Verified fix", second["message"])

    def test_commit_preparation_explains_paused_verification_and_keeps_staging_unchanged(self):
        original = self.model
        def model(*args):
            result = original(*args)
            if args[3] == "review":
                result.update(complete=False, blocking=["Real-Git snapshot verification is missing."])
            return result
        with patch.object(owner_providers, "invoke", side_effect=model):
            view = self.fix()
        worktree = Path(view["worktree"])
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        before = index.read_bytes()
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertIn("paused (0/1 tasks complete)", result["reason"])
        self.assertIn("Real-Git snapshot verification is missing", result["reason"])
        self.assertIn("resume with r", result["reason"])
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((worktree / "source.txt").read_text(), "fixed\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), self.head)

    def test_commit_preparation_keeps_conflicting_staging_byte_for_byte(self):
        view = self.fix()
        worktree = Path(view["worktree"])
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        for name in ("unrelated.txt", "source.txt"):
            with self.subTest(name=name):
                self.git("reset", "-q", "HEAD", cwd=worktree)
                (worktree / name).write_text("my staged change\n")
                self.git("add", name, cwd=worktree)
                before = index.read_bytes()
                result = self.request("owner-prepare-commit")
                self.assertFalse(result["ready"])
                self.assertIn("already staged", result["reason"])
                self.assertEqual(index.read_bytes(), before)
                self.assertFalse(index.with_name("index.lock").exists())

    def test_commit_preparation_keeps_busy_index_and_failed_updates_intact(self):
        view = self.fix()
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=view["worktree"]))
        lock = index.with_name("index.lock")
        before = index.read_bytes()
        lock.write_text("another Git operation")
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertEqual(lock.read_text(), "another Git operation")
        lock.unlink()
        real = owner_commit.run
        def fail_update(argv, **kwargs):
            if "update-index" in argv:
                raise ApiError("cancelled", "Cancelled before staging")
            return real(argv, **kwargs)
        with patch.object(owner_commit, "run", side_effect=fail_update), self.assertRaises(ApiError):
            self.request("owner-prepare-commit")
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse(lock.exists())

    def test_commit_preparation_checks_revision_and_execution_lock(self):
        view = self.fix()
        session = OwnerSession({"session": self.path})
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=view["worktree"]))
        before = index.read_bytes()
        with self.assertRaises(ApiError) as caught:
            dispatch({"protocol": 1, "command": "owner-prepare-commit", "session": self.path, "revision": -1})
        self.assertEqual(caught.exception.code, "stale_revision")
        with locked(session.execution_lock), self.assertRaises(ApiError) as caught:
            self.request("owner-prepare-commit")
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(index.read_bytes(), before)

    def test_commit_preparation_refuses_changed_context_and_in_progress_git(self):
        view = self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        state["batches"][-1]["discussion_stale"] = True
        session.save(state)
        self.assertFalse(self.request("owner-prepare-commit")["ready"])
        state = session.load()
        state["batches"][-1]["discussion_stale"] = False
        session.save(state)
        marker = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD", cwd=view["worktree"]))
        marker.write_text(self.base + "\n")
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertIn("Git operation", result["reason"])
        self.assertEqual(self.git("diff", "--cached", cwd=view["worktree"]), "")

    def test_commit_preparation_does_not_restage_already_committed_fix(self):
        view = self.fix()
        sha = self.commit_fix()
        for associate in (False, True):
            if associate:
                self.request("owner-associate")
            result = self.request("owner-prepare-commit", batch=view["batches"][-1]["id"])
            self.assertFalse(result["ready"])
            self.assertTrue(result["committed"])
            self.assertEqual(result["commits"], [sha])
            self.assertIn("already committed", result["reason"])
            self.assertNotIn("associate", result["reason"])
        self.assertTrue(self.request("owner-prepare-commit")["committed"])
        self.assertEqual(self.git("diff", "--cached", cwd=view["worktree"]), "")

    def test_commit_preparation_recognizes_committed_fix_beneath_later_commits(self):
        view = self.fix()
        sha = self.commit_fix()
        worktree = Path(view["worktree"])
        (worktree / "later.txt").write_text("later change\n")
        self.git("add", "later.txt", cwd=worktree)
        self.git("commit", "-qm", "Later work", cwd=worktree)
        (worktree / "staged.txt").write_text("keep staged\n")
        self.git("add", "staged.txt", cwd=worktree)
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        before = index.read_bytes()
        result = self.request("owner-prepare-commit")
        self.assertEqual(result["commits"], [sha])
        self.assertEqual(index.read_bytes(), before)

    def test_changed_head_does_not_imply_fix_committed_or_require_repeated_association(self):
        view = self.fix()
        worktree = Path(view["worktree"])
        self.git("add", "new.txt", cwd=worktree)
        self.git("commit", "-qm", "Partial fix", cwd=worktree)
        sha = self.git("rev-parse", "HEAD", cwd=worktree)
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertNotIn("committed", result)
        self.assertIn("HEAD changed", result["reason"])
        self.request("owner-associate", batch=view["batches"][-1]["id"], commits=[sha], items=["thread:T1"])
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertNotIn("committed", result)
        self.assertIn("Associated commits: " + sha[:12], result["reason"])
        self.assertNotIn("inspect or associate", result["reason"])

    def test_commit_preparation_handles_deletions_binary_modes_and_literal_names(self):
        original = self.model
        def model(session, state, batch, role, context):
            value = original(session, state, batch, role, context)
            if role == "fix":
                worktree = Path(state["worktree"])
                (worktree / "source.txt").unlink()
                unusual = worktree / ":(glob)*\tline\n.bin"
                unusual.write_bytes(b"\0binary\xff")
                unusual.chmod(0o755)
                (worktree / "link").symlink_to("new.txt")
                batch["checkpoint"] = owner_git.checkpoint(session, state, "literal paths")
            return value
        with patch.object(owner_providers, "invoke", side_effect=model):
            view = self.fix()
        result = self.request("owner-prepare-commit")
        self.assertTrue(result["ready"])
        self.assertEqual(self.git("write-tree", cwd=view["worktree"]), view["batches"][-1]["final_tree"])
        self.assertIn(":(glob)*\tline\n.bin", result["paths"])

    def test_prepared_commit_still_associates_exactly(self):
        view = self.fix()
        result = self.request("owner-prepare-commit")
        body = self.root / "message.txt"
        body.write_text(result["message"])
        self.git("commit", "-q", "-F", str(body), cwd=view["worktree"])
        sha = self.git("rev-parse", "HEAD", cwd=view["worktree"])
        self.metadata["headRefOid"] = sha
        self.git("update-ref", "refs/pull/7/head", sha)
        associated = self.request("owner-associate")
        self.assertEqual(associated["batches"][-1]["associations"]["thread:T1"]["commits"], [sha])

    def test_legacy_pushremote_migration_only_changes_the_generated_mapping(self):
        self.git("config", "remote.origin.pushurl", "git@github.com:org/repo.git")
        view = self.fix()
        session = OwnerSession({"session": self.path})
        for merge, removed in (("feature", True), ("manually-chosen", False)):
            with self.subTest(merge=merge):
                state = session.load()
                state["configured_target"].pop("method", None)
                session.save(state)
                self.git("config", f"branch.{view['branch']}.merge", "refs/heads/" + merge)
                self.git("config", f"branch.{view['branch']}.pushRemote", "origin")
                state = session.load()
                state["batches"][-1]["git_admin"] = owner_git.admin(session, state)
                session.save(state)
                self.request("owner-prepare-commit")
                entries = self.git("config", "--local", "--list").lower()
                self.assertEqual("pushremote=origin" not in entries, removed)
                self.assertEqual(self.git("config", f"branch.{view['branch']}.merge"), "refs/heads/" + merge)

    def test_commit_preparation_defaults_to_latest_batch_and_omits_skipped_feedback(self):
        view = self.fix()
        session = OwnerSession({"session": self.path})
        state = session.load()
        batch = state["batches"][-1]
        later = copy.deepcopy(batch)
        later.update(id="later", status="paused")
        state["batches"].append(later)
        session.save(state)
        self.assertFalse(self.request("owner-prepare-commit")["ready"])
        self.assertTrue(self.request("owner-prepare-commit", batch=batch["id"])["ready"])
        state = session.load()
        state["feedback"][0]["title"] = "Completed concern"
        state["feedback"][1]["title"] = "Skipped concern"
        batch["verified_items"] = ["thread:T1"]
        body = owner_commit.message(state, batch)
        self.assertIn("Completed concern", body)
        self.assertNotIn("Skipped concern", body)

    def test_commit_preparation_detects_an_index_writer_ignoring_git_lock(self):
        view = self.fix()
        index = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=view["worktree"]))
        real = owner_commit.run
        def change_index(argv, **kwargs):
            value = real(argv, **kwargs)
            if "update-index" in argv:
                index.write_bytes(b"external writer's index")
            return value
        with patch.object(owner_commit, "run", side_effect=change_index):
            result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertIn("index changed", result["reason"])
        self.assertEqual(index.read_bytes(), b"external writer's index")

    def test_cli_can_prepare_a_fix_without_committing(self):
        view = self.fix()
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = owner_cli(["prepare-commit", "--session", self.path])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ready"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=view["worktree"]), self.head)

    def test_new_push_mapping_preserves_preexisting_manual_configuration(self):
        view = self.fix()
        self.git("config", "remote.origin.pushurl", "git@github.com:org/repo.git")
        self.git("config", f"branch.{view['branch']}.remote", "origin")
        self.git("config", f"branch.{view['branch']}.merge", "refs/heads/manually-chosen")
        self.request("owner-prepare-commit")
        self.assertEqual(self.git("config", f"branch.{view['branch']}.merge"), "refs/heads/manually-chosen")
        self.assertNotIn("configured_target", self.view)

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
    def test_feedback_enums_do_not_constrain_task_dependencies(self):
        schema = owner_providers.output_schema("plan", ["thread:T1", "thread:T2"])
        fields = schema["properties"]["tasks"]["items"]["properties"]
        self.assertEqual(fields["items"]["items"]["enum"], ["thread:T1", "thread:T2"])
        self.assertEqual(fields["depends_on"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(fields["expected"]["items"]["properties"]["item"]["enum"], ["thread:T1", "thread:T2"])
        self.assertNotIn("enum", owner_providers.PLAN["properties"]["tasks"]["items"]["properties"]["items"]["items"])
        tasks = [self.task("t1", ["thread:T1"]), self.task("t2", ["thread:T2"], ["t1"])]
        self.assertEqual(len(owner_fixes.validate_plan({"tasks": tasks}, [{"key": "thread:T1"}, {"key": "thread:T2"}])), 2)

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
                self.assertIn("sandbox_mode=" + ("read-only" if role == "plan" else "workspace-write"), args)
                self.assertIn("approval_policy=never", args)
                if role == "review":
                    self.assertEqual(args[args.index("--cd") + 1], str(directory / "scratch"))
                    self.assertIn("--skip-git-repo-check", args)
                    self.assertIn("--ignore-rules", args)
                    self.assertIn("sandbox_workspace_write.writable_roots=[]", args)
                    self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", args)
                    self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", args)
                    self.assertIn("sandbox_workspace_write.network_access=false", args)
                elif role == "fix":
                    self.assertNotIn("--cd", args)
                    self.assertEqual(args[args.index("--add-dir") + 1], str(directory / "scratch"))
                else:
                    self.assertNotIn("--cd", args)
                    self.assertNotIn("--add-dir", args)
                choice["provider"] = "claude"
                args = owner_providers.argv(choice, Runtime(), role, directory, schema)
                self.assertEqual("Edit" in args, role != "fix")
                self.assertIn("Bash(git commit:*)", args)
                self.assertIn("Bash(git add:*)", args)


if __name__ == "__main__":
    unittest.main()
