"""PR selection is independent of saved review selection and model execution."""
import json
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_api
from pr_review_api import operations, pulls
from pr_review_api.common import ApiError, atomic_json, read_json, state_root
from pr_review_api.main import dispatch


class PullRequestsTest(unittest.TestCase):
    def setUp(self):
        self.f = test_api.ApiTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.git("remote", "add", "origin", "https://github.com/org/repo.git")

    def request(self, **fields):
        return dispatch({"protocol": 1, "command": "pull-requests", "cwd": str(self.f.repo), **fields})

    def pull(self, number, state="open", repository="github.com/org/repo"):
        return {"number": number, "title": f"Change {number}", "state": "closed" if state == "merged" else state,
                "merged_at": "2026-10-25" if state == "merged" else None,
                "html_url": f"https://{repository}/pull/{number}", "draft": False, "head": {"ref": "feature"}}

    def reply(self, value):
        return SimpleNamespace(stdout=json.dumps(value))

    def test_opening_only_reads_saved_state_and_identifies_repository(self):
        with patch.object(pulls, "command", side_effect=AssertionError("No network on open")):
            result = self.request()
        self.assertEqual(result["repository"], "github.com/org/repo")
        self.assertIsNone(result["checked_at"])
        self.assertEqual([p["pr"] for p in result["pull_requests"]], [7])
        self.assertEqual(result["pull_requests"][0]["state"], "UNKNOWN")
        self.assertFalse((state_root() / "repositories").exists())

    def test_refresh_discovers_unsaved_prs_on_all_pages_and_run_targets_new_pr(self):
        self.f.change("edit", comment="p1", body="Preserve old review")
        with patch.object(pulls, "command", side_effect=[
                self.reply([[self.pull(8)], [self.pull(9)]]), self.reply(self.pull(7, "merged"))]) as gh:
            result = self.request(refresh=True)
        args = gh.call_args_list[0].args[0]
        self.assertIn("--paginate", args)
        self.assertIn("--slurp", args)
        self.assertEqual(args[args.index("--method") + 1], "GET")
        self.assertEqual([p["pr"] for p in result["pull_requests"]], [9, 8, 7])
        self.assertEqual(result["pull_requests"][-1]["state"], "MERGED")
        self.assertFalse(result["pull_requests"][0]["sessions"])
        with patch.object(pulls, "command", side_effect=AssertionError("Cached open is local")):
            self.assertEqual(self.request(), result)
        with patch.object(operations, "stream") as run:
            dispatch({"protocol": 1, "command": "run", "cwd": str(self.f.repo),
                      "pr": 8, "repository": result["repository"]})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:4], ["8", "--repo", "github.com/org/repo"])
        self.assertNotIn("--resume", argv)
        self.assertEqual(dispatch(self.f.request())["comments"][0]["body"], "Preserve old review")

    def test_refresh_removes_closed_unsaved_prs_and_keeps_saved_history(self):
        with patch.object(pulls, "command", side_effect=[self.reply([[self.pull(8)]]), self.reply(self.pull(7))]):
            self.request(refresh=True)
        with patch.object(pulls, "command", side_effect=[self.reply([[]]), self.reply(self.pull(7, "closed"))]):
            result = self.request(refresh=True)
        self.assertEqual([p["pr"] for p in result["pull_requests"]], [7])
        self.assertEqual(result["pull_requests"][0]["state"], "CLOSED")
        self.assertTrue(result["pull_requests"][0]["sessions"])

    def test_failed_or_invalid_refresh_preserves_cached_list_and_drafts(self):
        with patch.object(pulls, "command", side_effect=[self.reply([[self.pull(8)]]), self.reply(self.pull(7))]):
            before = self.request(refresh=True)
        manifest = (self.f.path / "manifest.json").read_bytes()
        for failure in [ApiError("command_failed", "offline"), self.reply({"message": "forbidden"}),
                        self.reply([[self.pull(8, repository="github.com/other/repo")]])]:
            with patch.object(pulls, "command", side_effect=[failure]), self.assertRaises(ApiError):
                self.request(refresh=True)
            self.assertEqual(self.request(), before)
            self.assertEqual((self.f.path / "manifest.json").read_bytes(), manifest)
        # A detail request failing after successful pagination is also atomic.
        with patch.object(pulls, "command", side_effect=[self.reply([[]]), ApiError("command_failed", "offline")]), self.assertRaises(ApiError):
            self.request(refresh=True)
        self.assertEqual(self.request(), before)

    def test_multiple_remotes_require_selection_and_do_not_mix_same_number(self):
        self.f.git("remote", "add", "upstream", "git@github.com:other/repo.git")
        destination = state_root() / "github.com/other/repo/pr-7-test"
        shutil.copytree(self.f.path, destination)
        manifest = read_json(destination / "manifest.json")
        manifest.update(repository="github.com/other/repo", url="https://github.com/other/repo/pull/7")
        atomic_json(destination / "manifest.json", manifest)
        with patch.object(pulls, "command", side_effect=AssertionError("Select before network")):
            self.assertIsNone(self.request()["repository"])
            with self.assertRaises(ApiError):
                self.request(refresh=True)
            result = self.request(repository="github.com/other/repo")
        self.assertEqual(len(result["pull_requests"][0]["sessions"]), 1)
        self.assertEqual(result["pull_requests"][0]["sessions"][0]["session"], str(destination))

    def test_unsaved_repository_and_enterprise_remote_need_no_credentials_on_open(self):
        self.f.git("remote", "add", "enterprise", "ssh://git@github.example.com/org/another.git")
        with patch.object(pulls, "command", side_effect=AssertionError("No credentials needed")):
            result = self.request(repository="github.example.com/org/another")
        self.assertEqual(result["pull_requests"], [])
        with patch.object(pulls, "command", return_value=self.reply([[]])) as gh:
            self.request(repository="github.example.com/org/another", refresh=True)
        self.assertEqual(gh.call_args.args[0][3], "github.example.com")
