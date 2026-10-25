"""Reviewer lifecycle tests against saved real-Git sessions and mocked GitHub."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_api
from pr_review import workflow, publication, followup
from pr_review.common import ApiError, atomic_json, digest, read_json
from pr_review.api import dispatch


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.f = test_api.ApiTest()
        self.f.setUp()
        self.threads = [dict(key=f"thread:T{i}", kind="thread", root_id=100 + i,
                             path="source.txt", line=i, original_line=i,
                             is_resolved=False, is_outdated=i == 1, can_reply=True,
                             can_resolve=True, can_unresolve=True,
                             comments=[{"id": 100 + i, "author": "reviewer", "body": f"Concern {i}"}])
                        for i in (1, 2)]
        self.remote(self.threads)
        self.f.session.manifest = read_json(self.f.path / "manifest.json")
        state = self.f.session.load()
        state["discussion"] = {"items": copy.deepcopy(self.threads)}
        state["discussion_hash"] = digest(self.threads)
        self.f.session.save(state)

    def tearDown(self):
        self.f.tearDown()
        self.f.doCleanups()

    def remote(self, threads):
        atomic_json(self.f.path / "discussion/snapshot-a/discussion.json", {"items": threads})
        manifest = read_json(self.f.path / "manifest.json")
        manifest["discussion"]["hash"] = digest(threads)
        atomic_json(self.f.path / "manifest.json", manifest)

    def refresh(self):
        with patch.object(publication, "repository_operation"):
            return dispatch(self.f.request("refresh"))["review"]

    def request_state(self, **fields):
        thread = dispatch(self.f.request())["thread_contexts"]["thread:T1"]
        return self.f.request("thread-state", thread="thread:T1", resolved=True, expected=thread["expected"], **fields)

    def test_outdated_thread_is_unresolved_and_review_verdict_is_independent(self):
        self.f.change("verdict", verdict="REQUEST_CHANGES")
        atomic_json(self.f.path / "discussion/review-status.json",
                    {"available": True, "decision": "CHANGES_REQUESTED",
                     "opinionated": {"state": "CHANGES_REQUESTED"}, "latest_activity": {"state": "COMMENTED"}})
        review = dispatch(self.f.request())
        self.assertFalse(review["thread_contexts"]["thread:T1"]["thread"]["is_resolved"])
        self.assertTrue(review["thread_contexts"]["thread:T1"]["thread"]["is_outdated"])
        self.threads[0]["is_resolved"] = self.threads[1]["is_resolved"] = True
        self.remote(self.threads)
        review = self.refresh()
        self.assertEqual(review["review_status"]["opinionated"]["state"], "CHANGES_REQUESTED")
        self.assertEqual(review["verdict"], "REQUEST_CHANGES")

    def test_unrelated_resolution_preserves_staging_but_related_changes_require_inspection(self):
        self.f.change("placement", comment="p1", placement={"kind": "reply", "key": "thread:T1"})
        self.f.change("stage", comments=["p1", "p2"])
        self.threads[1]["is_resolved"] = True
        self.remote(self.threads)
        self.assertEqual([c["status"] for c in self.refresh()["comments"]], ["staged", "staged"])
        self.threads[0]["comments"].append({"id": 200, "body": "New evidence", "author": "author"})
        self.remote(self.threads)
        review = self.refresh()
        self.assertEqual([c["status"] for c in review["comments"]], ["unstaged", "unstaged"])
        self.assertTrue(all(c["needs_attention"] for c in review["comments"]))

    def test_other_thread_text_does_not_unstage_a_reply(self):
        self.f.change("placement", comment="p1", placement={"kind": "reply", "key": "thread:T1"})
        self.f.change("stage", comment="p1")
        self.threads[1]["comments"][0]["body"] = "New unrelated evidence"
        self.remote(self.threads)
        self.assertEqual(self.refresh()["comments"][0]["status"], "staged")

    def test_status_only_refresh_preserves_drafts(self):
        self.f.change("stage", comment="p1")
        atomic_json(self.f.path / "discussion/review-status.json", {"available": True, "decision": "APPROVED"})
        review = self.refresh()
        self.assertEqual(review["comments"][0]["status"], "staged")
        self.assertEqual(review["review_status"]["decision"], "APPROVED")

    def test_missing_code_context_keeps_the_conversation_available(self):
        self.threads[1]["line"] = 90000
        self.remote(self.threads)
        context = self.refresh()["thread_contexts"]["thread:T2"]
        self.assertIn("error", context)
        self.assertEqual(context["thread"]["comments"][0]["body"], "Concern 2")

    def test_status_changes_invalidate_confirmation_without_unstaging(self):
        self.f.change("stage", comment="p1")
        with patch.object(publication, "repository_operation"):
            prepared = dispatch(self.f.request("prepare"))
            atomic_json(self.f.path / "discussion/review-status.json", {"available": True, "decision": "CHANGES_REQUESTED"})
            with patch.object(publication, "gh") as posts:
                result = dispatch(self.f.request("publish", confirmation=prepared["confirmation"]))
            posts.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertIn("review status changed", result["reason"])
        self.assertEqual(result["review"]["comments"][0]["status"], "staged")

    def test_comment_activity_does_not_invalidate_opinionated_status(self):
        status = {"available": True, "decision": "CHANGES_REQUESTED", "opinionated": {"state": "CHANGES_REQUESTED", "id": "R1"}}
        atomic_json(self.f.path / "discussion/review-status.json", status)
        before = workflow.status_fingerprint(self.f.session)
        status.update(latest_activity={"state": "COMMENTED", "id": "R2"}, checked_at="later")
        atomic_json(self.f.path / "discussion/review-status.json", status)
        self.assertEqual(before, workflow.status_fingerprint(self.f.session))

    def test_stale_context_permission_denial_and_closed_pr_do_not_write(self):
        for scenario in ("head", "body", "permission", "closed"):
            with self.subTest(scenario=scenario):
                self.remote(self.threads)
                self.refresh()
                request = self.request_state()
                if scenario == "head":
                    atomic_json(self.f.path / "publication/pr-latest.json", {"headRefOid": "b" * 40, "state": "OPEN"})
                elif scenario == "closed":
                    atomic_json(self.f.path / "publication/pr-latest.json", {"headRefOid": self.f.head, "state": "CLOSED"})
                else:
                    changed = copy.deepcopy(self.threads)
                    if scenario == "body": changed[0]["comments"][0]["body"] += " Edited"
                    else: changed[0]["can_resolve"] = False
                    self.remote(changed)
                with patch.object(publication, "repository_operation"), patch.object(workflow, "command", return_value=SimpleNamespace(stdout="reviewer")), patch.object(workflow, "graphql") as mutation:
                    result = dispatch(request)
                self.assertFalse(result["changed"])
                mutation.assert_not_called()
                atomic_json(self.f.path / "publication/pr-latest.json", {"headRefOid": self.f.head, "state": "OPEN"})

    def test_ambiguous_resolution_reads_back_without_retry_or_changing_verdict(self):
        self.f.change("verdict", verdict="REQUEST_CHANGES")
        def mutate(*args):
            self.threads[0]["is_resolved"] = True
            self.remote(self.threads)
            raise ApiError("network", "Lost response")
        with patch.object(publication, "repository_operation"), patch.object(workflow, "command", return_value=SimpleNamespace(stdout="reviewer")), patch.object(workflow, "graphql", side_effect=mutate) as mutation:
            result = dispatch(self.request_state())
            self.assertTrue(result["changed"])
            self.assertFalse(result["uncertain"])
            self.assertEqual(result["review"]["verdict"], "REQUEST_CHANGES")
            again = dispatch(self.request_state())
            self.assertFalse(again["changed"])
            self.assertEqual(mutation.call_count, 1)

    def test_unconfirmed_mutation_is_not_retried(self):
        with patch.object(publication, "repository_operation"), patch.object(workflow, "command", return_value=SimpleNamespace(stdout="reviewer")), patch.object(workflow, "graphql", side_effect=ApiError("network", "Lost response")) as mutation:
            result = dispatch(self.request_state())
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["changed"])
        self.assertEqual(mutation.call_count, 1)

    def test_account_change_does_not_mutate(self):
        with patch.object(workflow, "command", return_value=SimpleNamespace(stdout="someone-else")), patch.object(workflow, "graphql") as mutation:
            with self.assertRaisesRegex(ApiError, "account changed"):
                dispatch(self.request_state())
        mutation.assert_not_called()

    def test_followup_requires_exact_coverage_and_keeps_uncertainty(self):
        self.f.session.manifest["followup_threads"] = ["thread:T1", "thread:T2"]
        result = {"assessments": [{"thread_key": f"thread:T{i}", "outcome": outcome,
                                   "evidence": evidence, "limitations": "Code tracing only", "suggested_reply": ""}
                                  for i, outcome, evidence in ((1, "STILL_PRESENT", "Replacement path still contains the bug"),
                                                               (2, "UNCERTAIN", "Second concern could not be reproduced"))]}
        self.assertEqual(followup.validate(self.f.session, result), result)
        duplicate = copy.deepcopy(result)
        duplicate["assessments"][1]["thread_key"] = "thread:T1"
        with self.assertRaises(ApiError): followup.validate(self.f.session, duplicate)
        result["assessments"][0]["evidence"] = ""
        with self.assertRaises(ApiError): followup.validate(self.f.session, result)

    def test_assessment_freshness_tracks_code_and_conversation(self):
        self.f.session.manifest["followup_threads"] = ["thread:T1"]
        atomic_json(self.f.path / "reviews/followup-input.json", followup.prepare(self.f.session))
        atomic_json(self.f.path / "reviews/followup.json", {"assessments": [{"thread_key": "thread:T1", "outcome": "ADDRESSED"}]})
        state = self.f.session.load()
        self.assertFalse(workflow.assessments(self.f.session, state)[0]["stale"])
        state["checked"] = {"head": "b" * 40}
        self.assertTrue(workflow.assessments(self.f.session, state)[0]["stale"])
        state["checked"] = None
        state["discussion"]["items"][0]["comments"][0]["body"] = "Additional concern"
        self.assertTrue(workflow.assessments(self.f.session, state)[0]["stale"])

    def test_followup_publication_discloses_limited_scope(self):
        manifest = read_json(self.f.path / "manifest.json")
        manifest.update(analysis_kind="followup", followup_threads=["thread:T1"], phases={"followup": {"status": "completed"}})
        atomic_json(self.f.path / "manifest.json", manifest)
        atomic_json(self.f.path / "reviews/followup.json", {"assessments": []})
        self.f.change("verdict", verdict="APPROVE")
        with patch.object(publication, "repository_operation"):
            result = dispatch(self.f.request("prepare"))
        self.assertTrue(result["ready"])
        self.assertEqual(result["verdict"], "APPROVE")
        self.assertIn("Limited coverage", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
