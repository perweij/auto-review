"""Commit a verified group in smaller steps, using real disposable Git repos."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_owner
from pr_review import owner_git, owner_commit_plan
from pr_review.owner_state import OwnerSession
from pr_review.owner_cli import main as owner_cli


class CommitPlanTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_owner.OwnerTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.request = self.fixture.request
        self.git = self.fixture.git
        self.keys = self.fixture.handoff(self.fixture.saved_review(), ["p1", "p2"])["items"]
        self.repair = False
        self.repaired = False
        self.no_second_change = False
        provider = patch("pr_review.owner_providers.invoke", side_effect=self.model)
        provider.start()
        self.addCleanup(provider.stop)

    def model(self, session, state, batch, role, context):
        keys = [i["key"] for i in context["feedback"]]
        if role == "plan":
            return {"tasks": [{"id": name, "items": items, "depends_on": deps, "rationale": name,
                               "expected": [{"item": key, "outcome": "Fixed"} for key in items]}
                              for name, items, deps in (("first", [self.keys[0], "thread:T1"], []),
                                                        ("second", [self.keys[1], "thread:T2"], ["first"]))]}
        if role == "fix":
            worktree = Path(state["worktree"])
            if context.get("scope"):
                (worktree / "source.txt").write_text("first fix\nsecond fix\ncombined correction\n")
                self.repaired = True
            elif self.keys[0] in keys:
                (worktree / "source.txt").write_text("first fix\n")
                (worktree / "first.txt").write_text("first regression test\n")
            elif not self.no_second_change:
                (worktree / "source.txt").write_text("first fix\nsecond fix\n")
                (worktree / "second.txt").write_text("second regression test\n")
            batch["checkpoint"] = owner_git.checkpoint(session, state, "mock implementation")
        blocking = ["Combined correction needed"] if role == "review" and context.get("scope") and self.repair and not self.repaired else []
        return {"summary": "Fix " + ("first" if self.keys[0] in keys else "second"), "complete": True,
                "outcomes": [{"item": key, "status": "fixed", "evidence": "Checked this concern"} for key in keys],
                "checks": ["Focused regression check"], "blocking": blocking}

    def fix(self):
        self.view = self.request("owner-fix", items=[self.keys[0], "thread:T1", self.keys[1], "thread:T2"])
        self.batch = self.view["batches"][-1]
        self.worktree = Path(self.view["worktree"])
        return self.batch["commit_plan"]

    def commit_prepared(self, prepared):
        self.assertTrue(prepared["ready"])
        self.git("commit", "-qm", prepared["message"], cwd=self.worktree)
        return self.git("rev-parse", "HEAD", cwd=self.worktree)

    def test_separate_commits_stage_saved_versions_even_in_the_same_file(self):
        plan = self.fix()
        self.assertEqual(len(plan["units"]), 2)
        final = (self.worktree / "source.txt").read_text()
        (self.worktree / "source.txt").write_text(final + "unrelated later edit\n")
        first = self.request("owner-prepare-commit", batch=self.batch["id"])
        self.assertEqual((first["position"], first["total"]), (1, 2))
        self.assertEqual(self.git("show", ":source.txt", cwd=self.worktree), "first fix")
        self.assertNotIn("second.txt", first["paths"])
        self.assertIn("Local concern 1", first["message"])
        self.assertNotIn("Local concern 2", first["message"])
        sha1 = self.commit_prepared(first)
        view = self.request("owner-reconcile")
        local = {i["key"]: i for i in view["feedback"]}
        self.assertEqual(local[self.keys[0]]["local_completion"]["commit"], sha1)
        self.assertTrue(local[self.keys[1]]["attention"])
        self.assertEqual(view["batches"][-1]["commit_progress"]["completed"], 1)
        self.assertNotIn("commit_match", view["batches"][-1])
        already = self.request("owner-prepare-commit", item=self.keys[0])
        self.assertTrue(already["committed"])
        self.assertEqual(already["commits"], [sha1])
        second = self.request("owner-prepare-commit", item=self.keys[1])
        self.assertEqual((second["position"], second["total"]), (2, 2))
        self.assertEqual(self.git("show", ":source.txt", cwd=self.worktree), final.strip())
        self.assertEqual(self.git("write-tree", cwd=self.worktree), self.batch["final_tree"])
        self.assertNotIn("Local concern 1", second["message"])
        sha2 = self.commit_prepared(second)
        view = self.request("owner-reconcile")
        self.assertEqual(view["batches"][-1]["commit_progress"]["commits"], [sha1, sha2])
        local = {i["key"]: i for i in view["feedback"]}
        self.assertEqual(local[self.keys[1]]["local_completion"]["commit"], sha2)
        self.assertIn("unrelated later edit", self.git("diff", cwd=self.worktree))
        self.assertTrue(self.request("owner-prepare-commit")["committed"])

    def test_publication_links_each_thread_to_its_own_verified_commit(self):
        self.fix()
        sha1 = self.commit_prepared(self.request("owner-prepare-commit"))
        # Only the first fix is pushed: its thread is independently publishable.
        self.git("update-ref", "refs/pull/7/head", sha1)
        self.fixture.metadata["headRefOid"] = sha1
        view = self.request("owner-associate")
        self.assertEqual([r["source"] for r in view["replies"]], ["thread:T1"])
        self.assertEqual(view["replies"][0]["commits"], [sha1])
        sha2 = self.commit_prepared(self.request("owner-prepare-commit"))
        self.git("update-ref", "refs/pull/7/head", sha2)
        self.fixture.metadata["headRefOid"] = sha2
        view = self.request("owner-associate")
        self.assertEqual({r["source"]: r["commits"] for r in view["replies"]},
                         {"thread:T1": [sha1], "thread:T2": [sha2]})
        self.assertFalse(self.fixture.posts)

    def test_later_unit_requires_earlier_commit_and_conflicting_staging_is_preserved(self):
        self.fix()
        later = self.request("owner-prepare-commit", item=self.keys[1])
        self.assertFalse(later["ready"])
        self.assertIn("Commit fix 1 of 2 first", later["reason"])
        self.assertFalse(self.git("diff", "--cached", cwd=self.worktree))
        self.git("add", "-A", cwd=self.worktree)
        index = self.git("write-tree", cwd=self.worktree)
        first = self.request("owner-prepare-commit")
        self.assertFalse(first["ready"])
        self.assertIn("already staged", first["reason"])
        self.assertEqual(self.git("write-tree", cwd=self.worktree), index)

    def test_cli_targets_one_logical_fix_without_silently_preparing_another(self):
        plan = self.fix()
        for selector in (["--unit", plan["units"][1]["id"]], ["--item", self.keys[1]]):
            with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
                code = owner_cli(["prepare-commit", "--session", self.fixture.path,
                                  "--batch", self.batch["id"], *selector])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertFalse(result["ready"])
            self.assertIn("Commit fix 1 of 2 first", result["reason"])
            self.assertFalse(self.git("diff", "--cached", cwd=self.worktree))
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            code = owner_cli(["prepare-commit", "--session", self.fixture.path,
                              "--item", self.keys[0], "--item", self.keys[1]])
        self.assertEqual(code, 1)
        self.assertIn("Choose one feedback item", errors.getvalue())

    def test_integration_repair_preserves_task_commits_and_requires_complete_series(self):
        self.repair = True
        plan = self.fix()
        self.assertEqual(plan["mode"], "tasks")
        self.assertEqual(len(plan["units"]), 3)
        self.assertEqual(plan["units"][-1]["kind"], "integration_repair")
        shas = []
        for position in range(3):
            prepared = self.request("owner-prepare-commit")
            self.assertEqual((prepared["position"], prepared["total"]), (position + 1, 3))
            if position == 2:
                self.assertIn("Integrate review fixes", prepared["message"])
            shas.append(self.commit_prepared(prepared))
            self.git("update-ref", "refs/pull/7/head", shas[-1])
            self.fixture.metadata["headRefOid"] = shas[-1]
            view = self.request("owner-associate")
            if position < 2:
                self.assertEqual(view["replies"], [])
                self.assertEqual(view["batches"][-1]["commit_progress"]["items"], {})
                self.assertFalse(view["batches"][-1]["commit_progress"]["complete"])
        self.assertEqual(self.git("show", shas[0] + ":source.txt"), "first fix")
        self.assertEqual(self.git("show", shas[1] + ":source.txt"), "first fix\nsecond fix")
        self.assertEqual(self.git("rev-parse", shas[-1] + "^{tree}"), self.batch["final_tree"])
        self.assertEqual({r["source"]: r["commits"] for r in view["replies"]},
                         {"thread:T1": [shas[0], shas[2]], "thread:T2": [shas[1], shas[2]]})

    def test_feedback_selection_advances_from_task_to_integration_repair(self):
        self.repair = True
        self.fix()
        self.commit_prepared(self.request("owner-prepare-commit", item=self.keys[0]))
        pending = self.request("owner-prepare-commit", item=self.keys[0])
        self.assertFalse(pending["ready"])
        self.assertIn("Commit fix 2 of 3 first", pending["reason"])
        self.commit_prepared(self.request("owner-prepare-commit", item=self.keys[1]))
        repair = self.request("owner-prepare-commit", item=self.keys[0])
        self.assertTrue(repair["ready"])
        self.assertIn("Integrate review fixes", repair["message"])

    def test_discontinuous_checkpoints_still_use_combined_fallback(self):
        self.repair = True
        self.fix()
        session = OwnerSession({"session": self.fixture.path})
        state = session.load()
        batch = state['batches'][-1]
        batch['tasks'][1]['before']['tree'] = batch['tasks'][0]['before']['tree']
        plan = owner_commit_plan.build(session, batch)
        self.assertEqual(plan['mode'], 'combined')
        self.assertEqual(len(plan['units']), 1)

    def test_started_legacy_combined_transaction_is_not_repartitioned(self):
        self.repair = True
        self.fix()
        session = OwnerSession({"session": self.fixture.path})
        state = session.load()
        batch = state['batches'][-1]
        batch['commit_plan'].update(mode='combined', reason='Legacy combined repair')
        batch['commit_execution'] = {'attempts': [{'unit': 'combined', 'status': 'running'}]}
        plan = owner_commit_plan.build(session, batch)
        self.assertEqual(plan['mode'], 'combined')
        self.assertEqual(plan['units'][0]['id'], 'combined')
        self.assertEqual(plan['units'][0]['tree'], batch['final_tree'])

    def test_shared_fix_without_another_patch_does_not_create_an_empty_commit(self):
        self.no_second_change = True
        plan = self.fix()
        self.assertEqual(len(plan["units"]), 1)
        self.assertEqual(set(plan["units"][0]["items"]), set(self.batch["items"]))
        sha = self.commit_prepared(self.request("owner-prepare-commit"))
        view = self.request("owner-reconcile")
        self.assertEqual(view["batches"][-1]["commit_progress"]["commits"], [sha])

    def test_unrelated_commit_between_units_stops_preparation_without_changing_index(self):
        self.fix()
        self.commit_prepared(self.request("owner-prepare-commit"))
        self.git("commit", "--allow-empty", "-qm", "unrelated commit", cwd=self.worktree)
        before = self.git("write-tree", cwd=self.worktree)
        result = self.request("owner-prepare-commit")
        self.assertFalse(result["ready"])
        self.assertIn("HEAD changed outside", result["reason"])
        self.assertEqual(self.git("write-tree", cwd=self.worktree), before)

    def test_ambiguous_prefix_does_not_stage_or_finish_findings(self):
        plan = self.fix()
        first = self.commit_prepared(self.request("owner-prepare-commit"))
        other = self.git("commit-tree", plan["units"][0]["tree"], "-p", self.batch["baseline"], "-m", "alternative")
        merge = self.git("commit-tree", plan["units"][0]["tree"], "-p", first, "-p", other, "-m", "merge alternatives")
        self.git("update-ref", "refs/heads/" + self.view["branch"], merge)
        before = self.git("write-tree", cwd=self.worktree)
        view = self.request("owner-reconcile")
        self.assertEqual(view["batches"][-1]["commit_progress"]["status"], "ambiguous")
        self.assertEqual(view["reconciliation"]["handled_items"], [])
        self.assertFalse(self.request("owner-prepare-commit")["ready"])
        self.assertEqual(self.git("write-tree", cwd=self.worktree), before)

    def test_amended_prefix_and_later_unrelated_commits_keep_exact_unit_mapping(self):
        self.fix()
        self.commit_prepared(self.request("owner-prepare-commit"))
        self.git("commit", "--amend", "-qm", "better first message", cwd=self.worktree)
        first = self.git("rev-parse", "HEAD", cwd=self.worktree)
        second = self.commit_prepared(self.request("owner-prepare-commit"))
        self.git("commit", "--allow-empty", "-qm", "later commit", cwd=self.worktree)
        view = self.request("owner-reconcile")
        self.assertEqual(view["batches"][-1]["commit_progress"]["commits"], [first, second])
        diff = self.request("owner-diff", batch=self.batch["id"], unit=self.batch["commit_plan"]["units"][0]["id"])
        self.assertIn("+first fix", diff["diff"])
        self.assertNotIn("second fix", diff["diff"])

    def test_existing_single_commit_still_finishes_every_unit(self):
        self.fix()
        sha = self.fixture.commit_fix(push=False)
        view = self.request("owner-reconcile")
        progress = view["batches"][-1]["commit_progress"]
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["commits"], [sha])
        self.assertEqual(progress["completed"], 2)
        self.assertEqual(view["reconciliation"]["handled_items"], self.keys)

    def test_dropping_last_commit_reopens_only_its_local_findings(self):
        self.fix()
        sha1 = self.commit_prepared(self.request("owner-prepare-commit"))
        self.commit_prepared(self.request("owner-prepare-commit"))
        self.request("owner-reconcile")
        self.git("update-ref", "refs/heads/" + self.view["branch"], sha1)
        view = self.request("owner-reconcile")
        self.assertEqual(view["reconciliation"]["reopened_items"], [self.keys[1]])
        self.assertEqual(view["batches"][-1]["commit_progress"]["completed"], 1)


if __name__ == "__main__":
    unittest.main()
