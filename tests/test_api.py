"""Behavioral API contracts using real Git snapshots and isolated saved state."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review_api.common import ApiError, atomic_json, digest, locked, read_json
from pr_review_api.main import dispatch
from pr_review_api.state import Session
from pr_review_api import publication


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                              "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                              "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                              "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.git("init", "-q", "-b", "main")
        (self.repo / "source.txt").write_text("first\nold\nthird\nfourth\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "source.txt").write_text("first\nnew\nthird\nfourth\n")
        self.git("commit", "-qam", "feature")
        self.head = self.git("rev-parse", "HEAD")
        self.path = self.root / "state/pr-review/github.com/org/repo/pr-7-test"
        self.manifest = {"format_version": 2, "repository": "github.com/org/repo", "pr": 7,
                         "url": "https://github.com/org/repo/pull/7", "git_common_dir": str(self.repo / ".git"),
                         "worktree": str(self.repo), "run_id": "pr-7-test", "status": "complete",
                         "created_at": "2026-01-01T00:00:00Z", "viewer": "reviewer",
                         "snapshot": {"head": self.head, "base": self.base, "merge_base": self.base},
                         "discussion": {"complete": True, "hash": "unchanged",
                                        "path": str(self.path / "discussion/snapshot-a/discussion.json")}}
        atomic_json(self.path / "manifest.json", self.manifest)
        atomic_json(self.path / "discussion/snapshot-a/discussion.json", {"items": []})
        atomic_json(self.path / "publication/pr-latest.json", {"headRefOid": self.head, "author": {"login": "author"}, "state": "OPEN"})
        self.findings = [{"id": f"F{i}", "title": f"Finding {i}", "validity": "CONFIRMED", "body_md": f"Comment {i}",
                          "evidence": "Checked actual code", "blocking": False,
                          "primary_location": {"path": "source.txt", "side": "RIGHT", "start_line": i, "line": i}, "locations": []}
                         for i in (1, 2)]
        atomic_json(self.path / "reviews/synthesis.json", {"recommendation": "COMMENT", "blocking_findings": 0, "findings": self.findings})
        proposals = [{"id": f"p{i}", "basis": {"finding_ids": [f"F{i}"], "body_md": f"Comment {i}"}, "classification": "NEW",
                      "destination": {"kind": "review"}, "body_md": f"Comment {i}", "selected": True, "published": False} for i in (1, 2)]
        atomic_json(self.path / "publication/contributions.json", {"proposals": proposals})
        atomic_json(self.path / "publication/actions.json", [])
        self.session = Session({"session": str(self.path)})

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()

    def request(self, command="read", **fields):
        return {"protocol": 1, "command": command, "session": str(self.path), "generation": self.session.id,
                "revision": digest(self.session.load()), **fields}

    def change(self, operation, **fields):
        return dispatch(self.request("mutate", operation=operation, **fields))

    def test_local_reads_are_stable_and_never_run_models(self):
        with patch.object(publication, "bridge", side_effect=AssertionError("Network forbidden")):
            first = dispatch(self.request())
            self.assertEqual(first["revision"], dispatch(self.request())["revision"])
            self.assertTrue(all(c["status"] == "unstaged" for c in first["comments"]))
            self.assertFalse((self.path / "publication/drafts.json").exists())
            found = dispatch({"protocol": 1, "command": "discover", "cwd": str(self.repo), "pr": 7})
            self.assertEqual(found["sessions"][0]["generation"], first["generation"])

    def test_edit_unstages_entire_comment_and_preserves_original_version(self):
        self.change("stage", comment="p1")
        self.change("edit", comment="p1", body="Comment 1")
        self.assertEqual(self.session.load()["comments"][0]["status"], "staged")
        self.change("edit", comment="p1", body="Human wording\nsecond paragraph")
        c = self.session.load()["comments"][0]
        self.assertEqual(c["status"], "unstaged")
        self.assertEqual(c["versions"][0]["body"], "Comment 1")
        self.assertEqual(self.git("diff", "--cached"), "")

    def test_legacy_severity_is_read_only_metadata_even_for_edited_staged_and_published_comments(self):
        synthesis_file = self.path / "reviews/synthesis.json"
        synthesis = read_json(synthesis_file)
        synthesis["report_md"] = "**F1 — Medium, blocking — CONFIRMED (Claude).**\n**F2 — Low, non-blocking**"
        synthesis["findings"][0]["blocking"] = True
        atomic_json(synthesis_file, synthesis)
        self.change("edit", comment="p1", body="My carefully edited High confidence wording")
        self.change("placement", comment="p1", placement={"kind": "review"})
        self.change("stage", comment="p1")
        state = self.session.load()
        state["comments"][1].update(status="published", receipt={"id": 123, "html_url": "https://example.test/receipt"})
        state["prepared"] = {"confirmation": "keep-me"}
        self.session.save(state)
        snapshots = {p: p.read_bytes() for p in (synthesis_file, self.session.file, self.path / "manifest.json")}
        with patch.object(publication, "bridge", side_effect=AssertionError("No network or models")):
            first = dispatch(self.request())
            second = dispatch(self.request())
        self.assertEqual(first["revision"], digest(state))
        self.assertEqual(first, second)
        self.assertEqual([c["severity"] for c in first["comments"]], ["Medium", "Low"])
        for saved, shown in zip(state["comments"], first["comments"]):
            self.assertEqual({key: shown[key] for key in saved}, saved)
            self.assertEqual(shown["finding_assessments"][0]["severity_source"], "report")
        self.assertTrue(first["comments"][0]["finding_assessments"][0]["blocking"])
        self.assertEqual(first["prepared"], state["prepared"])
        self.assertEqual(snapshots, {p: p.read_bytes() for p in snapshots})

    def test_severity_does_not_change_a_confirmed_publication_payload(self):
        self.change("edit", comment="p1", body="Exact human wording")
        self.change("stage", comment="p1")
        github, posts = self.fake_github()
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=github):
            prepared = dispatch(self.request("prepare"))
            before = self.session.file.read_bytes()
            synthesis_file = self.path / "reviews/synthesis.json"
            synthesis = read_json(synthesis_file)
            synthesis["findings"][0]["severity"] = "Critical"
            atomic_json(synthesis_file, synthesis)
            viewed = dispatch(self.request())
            self.assertEqual(viewed["comments"][0]["severity"], "Critical")
            self.assertEqual(viewed["revision"], prepared["review"]["revision"])
            self.assertEqual(self.session.file.read_bytes(), before)
            published = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
        self.assertTrue(published["complete"])
        self.assertEqual(posts[0][1]["comments"][0]["body"].split("\n\n<!--")[0], "Exact human wording")

    def test_initial_verdict_uses_model_suggestion_without_overwriting_saved_choice(self):
        synthesis = read_json(self.path / "reviews/synthesis.json")
        synthesis["recommendation"] = "REQUEST_CHANGES"
        atomic_json(self.path / "reviews/synthesis.json", synthesis)
        self.assertEqual(dispatch(self.request())["verdict"], "REQUEST_CHANGES")
        self.assertEqual(publication.make_actions(self.session, self.session.load()), [])
        self.change("stage", comment="p1")
        self.assertEqual(publication.make_actions(self.session, self.session.load())[0]["event"], "REQUEST_CHANGES")
        self.change("verdict", verdict="COMMENT")
        self.assertEqual(dispatch(self.request())["verdict"], "COMMENT")

    def test_own_pr_defaults_to_comment_and_explains_unavailable_verdicts(self):
        synthesis = read_json(self.path / "reviews/synthesis.json")
        synthesis["recommendation"] = "REQUEST_CHANGES"
        atomic_json(self.path / "reviews/synthesis.json", synthesis)
        atomic_json(self.path / "context/pr.json", {"author": {"login": "Reviewer"}})
        view = dispatch(self.request())
        self.assertEqual(view["verdict"], "COMMENT")
        self.assertEqual(view["recommendation"], "REQUEST_CHANGES")
        for verdict in view["verdicts"]:
            self.assertEqual(verdict["available"], verdict["value"] == "COMMENT")
            if not verdict["available"]:
                self.assertIn("PR author", verdict["reason"])

    def test_stale_writes_leave_existing_text_untouched(self):
        request = self.request("mutate", operation="edit", comment="p1", body="Stale text")
        self.change("edit", comment="p1", body="Current text")
        with self.assertRaisesRegex(ApiError, "saved draft changed"):
            dispatch(request)
        self.assertEqual(self.session.load()["comments"][0]["body"], "Current text")

    def test_base_metadata_advancement_keeps_generation_and_human_drafts(self):
        self.change("edit", comment="p1", body="Retain my edit")
        self.change("stage", comment="p1")
        identity = self.session.id
        manifest = read_json(self.path / "manifest.json")
        manifest["snapshot"]["base"] = "f" * 40
        atomic_json(self.path / "manifest.json", manifest)
        self.session = Session({"session": str(self.path)})
        self.assertEqual(self.session.id, identity)
        c = dispatch(self.request())["comments"][0]
        self.assertEqual(c["body"], "Retain my edit")
        self.assertEqual(c["status"], "staged")

    def test_explicit_resume_keeps_saved_model_choices_unless_overridden(self):
        from pr_review_api import operations
        manifest = read_json(self.path / "manifest.json")
        manifest["models"] = {"claude": "claude-haiku-5-5", "codex": "gpt-6-luna"}
        atomic_json(self.path / "manifest.json", manifest)
        with patch.object(operations, "stream") as run:
            dispatch(self.request("run"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--claude-model")+1], "claude-haiku-5-5")
            self.assertEqual(argv[argv.index("--codex-model")+1], "gpt-6-luna")
            dispatch(self.request("run", codex_model="explicit-model"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--codex-model")+1], "explicit-model")

    def test_synthesis_testing_policy_is_explicit_and_validated_before_running(self):
        from pr_review_api import operations
        with patch.object(operations, "stream") as run:
            dispatch(self.request("run", synthesis_tests="full"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--synthesis-tests")+1], "full")
            dispatch(self.request("run"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--synthesis-tests")+1], "focused")
            run.reset_mock()
            for invalid in ("all", "", None, True, ["full"]):
                with self.subTest(invalid=invalid), self.assertRaises(ApiError) as caught:
                    dispatch(self.request("run", synthesis_tests=invalid))
                self.assertEqual(caught.exception.code, "invalid_request")
            run.assert_not_called()

    def test_setup_resolves_saved_and_pending_configuration_without_mutating_drafts(self):
        from pr_review_api.settings import DEFAULTS
        from pr_review_api import operations
        config = {**DEFAULTS, "reviewers": ["codex"], "processing_provider": "codex",
                  "codex_effort": "low", "synthesis_tests": "full"}
        self.manifest["configuration"] = config
        atomic_json(self.path / "manifest.json", self.manifest)
        before = (self.path / "manifest.json").read_bytes()
        resolved = dispatch(self.request("settings", defaults={"codex_effort": "high"}))
        self.assertEqual(resolved["settings"], config)
        self.assertEqual((self.path / "manifest.json").read_bytes(), before)
        pending = Path(str(self.path) + "-history/attempt-new")
        pending_config = {**config, "codex_model": "another-model", "codex_effort": "medium"}
        atomic_json(pending / "manifest.json", {**self.manifest, "configuration": pending_config})
        (self.path / ".pending-review").symlink_to(pending)
        self.assertEqual(dispatch(self.request("settings"))["settings"], pending_config)
        with patch.object(operations, "stream") as run:
            dispatch(self.request("run"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--reviewers")+1], "codex")
            self.assertEqual(argv[argv.index("--codex-effort")+1], "medium")
            self.assertEqual(argv[argv.index("--synthesis-tests")+1], "full")

    def test_new_review_uses_defaults_without_changing_existing_review(self):
        result = dispatch({"protocol": 1, "command": "settings", "cwd": str(self.repo), "pr": 8,
                           "defaults": {"reviewers": ["claude"], "claude_model": "haiku", "claude_effort": "low"}})
        self.assertIsNone(result["session"])
        self.assertEqual(result["settings"]["reviewers"], ["claude"])
        self.assertEqual(result["settings"]["claude_model"], "haiku")
        self.assertEqual(result["settings"]["processing_provider"], "claude")
        self.assertNotIn("configuration", read_json(self.path / "manifest.json"))

    def test_cli_settings_reader_keeps_explicit_external_resume_supported(self):
        from pr_review_api.settings import DEFAULTS
        external = self.root / "external-review/manifest.json"
        configuration = {**DEFAULTS, "reviewers": ["codex"], "processing_provider": "codex", "codex_effort": "low"}
        atomic_json(external, {**self.manifest, "configuration": configuration})
        script = Path(__file__).resolve().parents[1] / "lib/review-settings.py"
        result = subprocess.run([sys.executable, script], input=json.dumps({"manifest": str(external)}),
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), configuration)

    def test_api_rejects_unsupported_menu_selection_before_starting(self):
        from pr_review_api import operations
        with patch.object(operations, "stream") as run, self.assertRaises(ApiError):
            dispatch(self.request("run", reviewers=["codex"], codex_model="fable", catalogs={
                "codex": {"provider": "codex", "models": [{"value": "gpt-6-luna", "efforts": ["high"]}]}}))
        run.assert_not_called()

    def test_adaptation_accepts_codex_events_and_preserves_draft_until_accepted(self):
        from pr_review_api import operations
        state = self.session.load()
        before = copy.deepcopy(state)
        (self.path / "logs").mkdir(exist_ok=True)
        result_events = '{"type":"item.completed","item":{"type":"agent_message","text":"A contextual reply"}}\n'
        with patch.object(self.session, "comment_context", return_value={"thread": {"comments": ["Already fixed"]}}), \
             patch.object(operations, "stream", return_value=result_events):
            result = operations.adapt(self.session, state, self.request("adapt", comment="p1"))
        self.assertEqual(result["body"], "A contextual reply")
        self.assertEqual(state, before)

    def test_diff_side_and_range_validation(self):
        context = self.session.git
        self.assertEqual(context.anchor({"path": "source.txt", "side": "LEFT", "line": 2})["line"], 2)
        for anchor in ({"path": "source.txt", "side": "LEFT", "line": 1},
                       {"path": "source.txt", "side": "RIGHT", "line": 99},
                       {"path": "../source.txt", "line": 2},
                       {"path": "source.txt", "side": "LEFT", "start_line": 1, "line": 2}):
            with self.assertRaises(ApiError):
                context.anchor(anchor)
        rows = context.context({"path": "source.txt", "side": "RIGHT", "line": 2})["rows"]
        self.assertEqual([r["text"] for r in rows if r["highlight"]], ["new"])
        self.assertEqual(context.context({"path": "source.txt", "side": "LEFT", "line": 2})["commit"], self.base)
        self.assertIn("old", context.source("source.txt", "LEFT")["text"])

    def test_rename_uses_new_path_and_original_left_blob(self):
        self.git("mv", "source.txt", "renamed.txt")
        self.git("commit", "-qm", "rename")
        manifest = copy.deepcopy(self.manifest)
        manifest["snapshot"]["head"] = self.git("rev-parse", "HEAD")
        from pr_review_api.context import GitContext
        context = GitContext(manifest)
        self.assertEqual(context.files()[0]["old_path"], "source.txt")
        self.assertIn("old", context.source("renamed.txt", "LEFT")["text"])
        self.assertEqual(context.anchor({"path": "renamed.txt", "line": 2})["path"], "renamed.txt")

    def test_suppression_restore_and_bulk_skip(self):
        state = self.session.load()
        state["comments"][0]["classification"] = "UNCERTAIN"
        self.session.save(state)
        result = self.change("stage", comments=["p1", "p2"], bulk=True)
        self.assertEqual(result["changed"], ["p2"])
        self.assertEqual(result["skipped"][0]["id"], "p1")
        self.change("stage", comment="p1")
        self.change("suppress", comment="p1")
        self.change("restore", comment="p1")
        self.assertEqual(self.session.load()["comments"][0]["status"], "unstaged")

    def test_placement_override_keeps_text_and_unstages(self):
        self.change("edit", comment="p1", body="Human wording")
        self.change("stage", comment="p1")
        self.change("placement", comment="p1", placement={"kind": "inline", "path": "source.txt", "side": "LEFT", "line": 2})
        c = self.session.load()["comments"][0]
        self.assertTrue(c["override"])
        self.assertEqual(c["body"], "Human wording")
        self.assertEqual(c["status"], "unstaged")
        self.change("automatic", comment="p1")
        self.assertFalse(self.session.load()["comments"][0]["override"])

    def test_new_generation_is_explicit_and_old_edits_recoverable(self):
        self.change("edit", comment="p1", body="Hours of human work")
        old = self.session.id
        candidate = Path(str(self.path) + "-history") / "attempt-new"
        shutil.copytree(self.path, candidate)
        manifest = read_json(candidate / "manifest.json")
        manifest["session_directory"] = str(self.path)
        atomic_json(candidate / "manifest.json", manifest)
        (self.path / ".current-review").symlink_to(candidate)
        old_view = dispatch(self.request())
        self.assertTrue(old_view["historical"])
        self.assertEqual(old_view["comments"][0]["body"], "Hours of human work")
        self.session = Session({"session": str(self.path)})
        current = dispatch(self.request())
        self.assertEqual(current["comments"][0]["body"], "Comment 1")
        result = self.change("recover", source_generation=old, source_comment="p1")
        recovered = result["review"]["comments"][-1]
        self.assertEqual(recovered["body"], "Hours of human work")
        self.assertEqual(recovered["placement"]["kind"], "unplaced")
        self.assertEqual(recovered["status"], "unstaged")
        self.assertEqual(len(dispatch(self.request("history"))["generations"]), 2)

    def test_generation_and_body_versions_survive_git_gc(self):
        self.change("edit", comment="p1", body="Saved wording")
        refs = self.git("for-each-ref", "--format=%(refname)", "refs/pr-review/api/")
        self.assertIn("merge-base", refs)
        self.assertIn("head", refs)

    def test_promotion_waits_for_an_in_progress_draft_save(self):
        import hashlib
        self.change("edit", comment="p1", body="Before promotion")
        candidate = Path(str(self.path) + "-history") / "attempt-promotion"
        shutil.copytree(self.path, candidate)
        manifest = read_json(candidate / "manifest.json")
        manifest.update(session_directory=str(self.path), attempt={"status": "building"}, phases={})
        for stage, name in (("claude", "claude-independent.md"), ("codex", "codex.md"),
                            ("synthesis", "synthesis.json"), ("contributions", "contributions.json")):
            output = candidate / "reviews" / name
            if not output.exists(): output.write_text("Saved output\n")
            manifest["phases"][stage] = {"status": "completed", "output_hash": hashlib.sha256(output.read_bytes()).hexdigest()}
        atomic_json(candidate / "manifest.json", manifest)
        (self.path / ".pending-review").symlink_to(candidate)
        project = Path(__file__).resolve().parents[1]
        script = 'source "$1/lib/core.sh"; source "$1/lib/attempts.sh"; SESSION_DIR=$2; RUN_DIR=$3; CONTRIBUTIONS_READY=true; commit_review_attempt'
        with locked(self.session.lock):
            child = subprocess.Popen(["bash", "-c", script, "promotion-test", str(project), str(self.path), str(candidate)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.25)
                self.assertIsNone(child.poll())
                self.assertFalse((self.path / ".current-review").exists())
                state = self.session.load()
                state["comments"][0]["body"] = "Saved during promotion"
                self.session.save(state)
            except BaseException:
                child.terminate()
                child.communicate(timeout=3)
                raise
        output, error = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, output + error)
        self.assertEqual((self.path / ".current-review").resolve(), candidate)
        self.assertEqual(self.session.load()["comments"][0]["body"], "Saved during promotion")
        with self.assertRaisesRegex(ApiError, "newer review is current"):
            self.change("edit", comment="p1", body="Stale write")

    def test_refresh_unstages_without_rewriting_manual_work(self):
        self.change("placement", comment="p1", placement={"kind": "review"})
        self.change("stage", comment="p1")
        manifest = read_json(self.path / "manifest.json")
        manifest["discussion"]["hash"] = "different"
        atomic_json(self.path / "manifest.json", manifest)
        with patch.object(publication, "bridge"):
            result = dispatch(self.request("refresh"))
        c = result["review"]["comments"][0]
        self.assertEqual(c["placement"], {"kind": "review"})
        self.assertEqual(c["body"], "Comment 1")
        self.assertEqual(c["status"], "unstaged")
        self.assertTrue(c["needs_attention"])

    def fake_github(self, fail_reply=False, ambiguous_review=False):
        posts, reviews, replies, inline = [], [], [], []
        def call(session, endpoint, payload=None, pages=False):
            if payload is None:
                if endpoint == "reviews": return reviews
                if endpoint == "comments": return replies
                if endpoint.endswith("/comments"): return inline
                raise AssertionError(endpoint)
            posts.append((endpoint, payload))
            if endpoint.endswith("/replies"):
                if fail_reply:
                    raise ApiError("network", "Connection lost")
                receipt = {"id": 101, "body": payload["body"], "user": {"login": "reviewer"},
                           "in_reply_to_id": 88, "pull_request_review_id": 201}
                replies.append(receipt)
                return receipt
            receipt = {"id": 200, "body": payload["body"], "commit_id": self.head,
                       "user": {"login": "reviewer"}, "state": {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[payload["event"]]}
            reviews.append(receipt)
            inline.extend({"id": 300+i, "body": c["body"], "pull_request_review_id": 200, "user": {"login": "reviewer"}}
                          for i, c in enumerate(payload.get("comments", [])))
            if ambiguous_review:
                raise ApiError("network", "Response lost after server accepted review")
            return receipt
        return call, posts

    def test_exact_inline_drafts_and_parent_comment_receipts(self):
        self.change("edit", comment="p1", body="Precisely this wording\n\nNo rewritten summary")
        self.change("stage", comments=["p1", "p2"])
        github, posts = self.fake_github()
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=github):
            prepared = dispatch(self.request("prepare"))
            self.assertTrue(prepared["ready"])
            published = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
        self.assertTrue(published["complete"])
        self.assertEqual(len(posts), 1)
        payload = posts[0][1]
        self.assertEqual(len(payload["comments"]), 2)
        self.assertEqual(payload["comments"][0]["body"].split("\n\n<!--")[0], "Precisely this wording\n\nNo rewritten summary")
        self.assertTrue(all(c["receipt"]["id"] >= 300 for c in published["review"]["comments"]))
        self.assertTrue(payload["body"].strip().startswith("<!--"))

    def test_ambiguous_review_recovery_never_repeats_verdict(self):
        self.change("verdict", verdict="REQUEST_CHANGES")
        self.change("stage", comment="p1")
        github, posts = self.fake_github(ambiguous_review=True)
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=github):
            prepared = dispatch(self.request("prepare"))
            result = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
            self.assertFalse(result["complete"])
            with self.assertRaisesRegex(ApiError, "previous submission"):
                self.change("edit", comment="p1", body="Changed before resolving")
            resolved = dispatch(self.request("resolve"))
            self.assertEqual(resolved["review"]["comments"][0]["status"], "published")
            prepared = dispatch(self.request("prepare"))
            self.assertFalse(prepared["ready"])
        self.assertEqual(len(posts), 1)

    def test_verdict_only_and_expired_confirmation(self):
        self.change("verdict", verdict="APPROVE")
        github, posts = self.fake_github()
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=github):
            prepared = dispatch(self.request("prepare"))
            self.assertEqual(prepared["counts"]["inline"], 0)
            self.change("verdict", verdict="COMMENT")
            with self.assertRaisesRegex(ApiError, "confirm it again"):
                dispatch(self.request("publish", confirmation=prepared["confirmation"]))
        self.assertEqual(posts, [])

    def test_explicit_comment_verdict_without_findings_or_assessment(self):
        self.change("verdict", verdict="COMMENT")
        github, posts = self.fake_github()
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=github):
            prepared = dispatch(self.request("prepare"))
            self.assertTrue(prepared["ready"])
            result = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
            self.assertTrue(result["complete"])
            self.assertFalse(dispatch(self.request("prepare"))["ready"])
        self.assertEqual(len(posts), 1)
        self.assertNotIn("comments", posts[0][1])

    def test_partial_reply_then_review_retry_preserves_bodies_and_receipts(self):
        state = self.session.load()
        state["discussion"]["items"] = [{"key": "thread:test", "kind": "thread", "root_id": 88,
                                           "can_reply": True, "path": "source.txt", "line": 2, "comments": []}]
        atomic_json(self.path / "discussion/snapshot-a/discussion.json", state["discussion"])
        self.session.save(state)
        self.change("placement", comment="p1", placement={"kind": "reply", "key": "thread:test"})
        self.change("stage", comments=["p1", "p2"])
        github, posts = self.fake_github()
        failures = []
        def flaky(*args, **kwargs):
            if args[1] == "reviews" and len(args) > 2 and not failures:
                failures.append(True)
                raise ApiError("network", "Rejected before acceptance")
            return github(*args, **kwargs)
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=flaky):
            prepared = dispatch(self.request("prepare"))
            result = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
            self.assertFalse(result["complete"])
            self.assertEqual([c["status"] for c in result["review"]["comments"]], ["published", "staged"])
            prepared = dispatch(self.request("prepare"))
            self.assertEqual(prepared["counts"], {"inline": 1, "replies": 0, "overall": 0})
            result = dispatch(self.request("publish", confirmation=prepared["confirmation"]))
            self.assertTrue(result["complete"])
        self.assertEqual(len(posts), 2)
        self.assertEqual(len([a for a in read_json(self.path / "publication/actions.json") if a["status"] == "published"]), 2)

    def test_late_recovered_review_invalidates_an_already_prepared_retry(self):
        self.change("verdict", verdict="REQUEST_CHANGES")
        self.change("stage", comment="p1")
        github, posts = self.fake_github(ambiguous_review=True)
        hidden = [True]
        def delayed(*args, **kwargs):
            values = github(*args, **kwargs)
            if args[1] == "reviews" and len(args) == 2 and hidden[0]:
                return []
            return values
        with patch.object(publication, "bridge"), patch.object(publication, "gh", side_effect=delayed):
            prepared = dispatch(self.request("prepare"))
            dispatch(self.request("publish", confirmation=prepared["confirmation"]))
            retry = dispatch(self.request("prepare"))
            self.assertTrue(retry["ready"])
            hidden[0] = False
            result = dispatch(self.request("publish", confirmation=retry["confirmation"]))
            self.assertFalse(result["complete"])
            self.assertEqual(result["review"]["comments"][0]["status"], "published")
        self.assertEqual(len(posts), 1)

    def test_protocol_is_machine_readable_and_rejects_unknown_versions(self):
        cli = Path(__file__).resolve().parents[1] / "bin/pr-review"
        result = subprocess.run([str(cli), "api"], input=json.dumps({"protocol": 99, "command": "read"}) + "\n",
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "protocol_mismatch")
        self.assertEqual(result.stderr, "")


class OperationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_startup_failure_retains_reason_and_private_log_without_a_review(self):
        from pr_review_api import operations
        for trailing in ("", "\nStatus: Failed\n"):
            with self.subTest(trailing=trailing), patch.object(operations, "emit") as emit:
                script = "import sys; print('[1/7] Checking authentication'); sys.stderr.write(" + repr(
                    "Error: GitHub authentication failed; run gh auth login" + trailing) + "); sys.exit(1)"
                with self.assertRaises(ApiError) as caught:
                    operations.stream([sys.executable, "-c", script], self.root)
                error = caught.exception
                self.assertEqual(error.code, "operation_failed")
                self.assertEqual(str(error), "GitHub authentication failed; run gh auth login")
                log = Path(error.details["log_path"])
                self.assertIn("[1/7] Checking authentication", log.read_text())
                self.assertIn("GitHub authentication failed", log.read_text())
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                record = read_json(log.with_suffix(".json"))
                self.assertEqual(record["exit_code"], 1)
                self.assertEqual(record["status"], "failed")
                started = next(c for c in emit.call_args_list if c.args[0] == "started")
                self.assertEqual(started.kwargs["log_path"], str(log))
                self.assertEqual(started.kwargs["operation_id"], error.details["operation_id"])

    def test_captured_response_is_returned_without_becoming_progress(self):
        from pr_review_api import operations
        with patch.object(operations, "emit") as emit:
            result = operations.stream([sys.executable, "-c",
                                        "import sys; sys.stdout.write('Private draft'); sys.stderr.write('Diagnostic')"],
                                       self.root, capture=True)
        self.assertEqual(result, "Private draft")
        log = next((self.root / "pr-review/operations").glob("*.log"))
        self.assertEqual(log.read_text(), "[stderr] Diagnostic\n")
        self.assertEqual(read_json(log.with_suffix(".json"))["exit_code"], 0)
        self.assertFalse(any("Private draft" in str(c) for c in emit.call_args_list))


if __name__ == "__main__":
    unittest.main()
