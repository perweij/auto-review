"""Trust regressions using real configuration files and no Git mutations/providers."""
import copy
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review.common import ApiError
from pr_review import owner_fixes, owner_git, owner_providers


class ConfigurationTrustTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = {"worktree": str(self.root), "metadata": {"baseRefOid": "base", "headRefOid": "head"}}
        self.changed = []
        self.tree_patch = ""
        self.session = SimpleNamespace(repo=SimpleNamespace(git=Mock(side_effect=self.git), common=self.root, pr=1),
                                       path=self.root, save=Mock())
        # Even an ignored directory is scanned; Git reports no configuration in the tree.
        self.write(".gitignore", "CLAUDE.local.md\n.claude/\nignored/\n")

    def git(self, *args, **kwargs):
        if args[0] == "merge-base":
            output = "base\n"
        elif args[:2] == ("diff", "--name-only"):
            output = "\0".join(self.changed)
        elif args[0] == "ls-tree":
            output = "entry for " + args[-1]
        elif args[0] == "diff":
            output = self.tree_patch
        elif args == ("rev-parse", "HEAD"):
            output = "head\n"
        else:
            self.fail(f"Unexpected Git command (mutations forbidden): {args}")
        return SimpleNamespace(returncode=0, stdout=output)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def required(self, approval=None):
        with self.assertRaises(ApiError) as caught:
            owner_git.trust(self.session, self.state, "tree", approval=approval)
        self.assertEqual(caught.exception.code, "trust_required")
        return caught.exception.details

    def approve(self):
        details = self.required()
        owner_git.trust(self.session, self.state, "tree", approval=details["trusted_snapshot"])
        return details["trusted_snapshot"]

    def test_ignored_configuration_additions_and_edits_change_token_and_diff(self):
        owner_git.trust(self.session, self.state, "tree")
        for name in ("CLAUDE.local.md", ".claude/settings.local.json"):
            with self.subTest(name=name):
                path = self.write(name, "original instructions\n")
                added = self.required()
                self.assertIn(name, added["paths"])
                self.assertIn("+original instructions", added["diff"])
                token = self.approve()
                path.write_text("revised instructions\n")
                edited = self.required()
                self.assertNotEqual(token, edited["trusted_snapshot"])
                self.assertIn("-original instructions", edited["diff"])
                self.assertIn("+revised instructions", edited["diff"])
                self.approve()

    def test_deletion_of_last_ignored_file_requires_approval(self):
        for name in ("CLAUDE.local.md", ".claude/settings.local.json"):
            with self.subTest(name=name):
                path = self.write(name, "approved instructions\n")
                token = self.approve()
                path.unlink()
                deleted = self.required()
                self.assertEqual(deleted["paths"], [name])
                self.assertNotEqual(token, deleted["trusted_snapshot"])
                self.assertIn("-approved instructions", deleted["diff"])
                self.assertIn("+++ /dev/null", deleted["diff"])
                self.approve()
                owner_git.trust(self.session, self.state, "tree")

    def test_stale_approval_does_not_replace_approved_contents(self):
        path = self.write("CLAUDE.local.md", "approved\n")
        self.approve()
        previous = copy.deepcopy(self.state)
        path.write_text("inspected\n")
        stale = self.required()["trusted_snapshot"]
        path.write_text("not inspected\n")
        fresh = self.required(approval=stale)
        self.assertNotEqual(stale, fresh["trusted_snapshot"])
        self.assertIn("-approved", fresh["diff"])
        self.assertIn("+not inspected", fresh["diff"])
        self.assertEqual(self.state, previous)

    def test_unrelated_source_edits_preserve_trust_and_persisted_configuration(self):
        self.write(".claude/settings.local.json", '{"permissions": {}}\n')
        self.approve()
        previous = copy.deepcopy(self.state)
        self.write("source.py", "new source\n")
        self.changed = ["source.py"]
        self.session.save.reset_mock()
        owner_git.trust(self.session, self.state, "different-source-tree")
        self.assertEqual(previous, self.state)
        self.session.save.assert_not_called()

    def test_legacy_token_without_configuration_requires_renewed_confirmation(self):
        self.write("CLAUDE.local.md", "local instructions\n")
        self.approve()
        del self.state["trusted_configuration"]
        details = self.required()
        self.assertIn("+local instructions", details["diff"])
        self.approve()
        owner_git.trust(self.session, self.state, "tree")

    def test_existing_path_rules_include_nested_ignored_configurations(self):
        names = ["ignored/AGENTS.md", "ignored/AGENTS.override.md", "ignored/CLAUDE.md",
                 "ignored/CLAUDE.local.md", "ignored/.mcp.json", "ignored/.codex/config.toml",
                 "ignored/.claude/rules/policy.md"]
        for name in reversed(names):
            self.write(name, name + "\n")
        details = self.required()
        self.assertEqual(details["paths"], sorted(names))
        self.assertEqual(details["trusted_snapshot"], self.required()["trusted_snapshot"])

    def test_tracked_configuration_deletion_still_displays_tree_diff(self):
        self.changed = ["AGENTS.md"]
        self.tree_patch = "--- a/AGENTS.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-base instructions\n"
        details = self.required()
        self.assertEqual(details["paths"], ["AGENTS.md"])
        self.assertIn("-base instructions", details["diff"])
        self.approve()
        owner_git.trust(self.session, self.state, "tree")

    def test_unrelated_directory_cycle_preserves_configuration_checks(self):
        self.write(".gitignore", "node_modules/\nCLAUDE.local.md\n")
        dependencies = self.root / "node_modules"
        dependencies.mkdir()
        (dependencies / "self").symlink_to(self.root, target_is_directory=True)
        owner_git.trust(self.session, self.state, "tree")
        path = self.write("CLAUDE.local.md", "approved\n")
        self.approve()
        path.write_text("changed\n")
        details = self.required()
        self.assertEqual(details["paths"], ["CLAUDE.local.md"])
        self.assertIn("-approved", details["diff"])
        self.assertIn("+changed", details["diff"])
        self.approve()
        path.unlink()
        self.assertIn("Configuration deleted", self.required()["diff"])

    def test_linked_configuration_directory_is_still_inspected(self):
        with tempfile.TemporaryDirectory() as external:
            path = Path(external) / "settings.local.json"
            path.write_text("approved\n")
            (self.root / ".claude").symlink_to(external, target_is_directory=True)
            self.approve()
            path.write_text("changed\n")
            details = self.required()
            self.assertEqual(details["paths"], [".claude/settings.local.json"])
            self.assertIn("+changed", details["diff"])

    def test_configuration_directory_cycle_still_fails_closed(self):
        (self.root / ".claude").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ApiError) as caught:
            owner_git.trust(self.session, self.state, "tree")
        self.assertEqual(caught.exception.code, "configuration_unreadable")
        self.assertNotIn("trusted_snapshot", self.state)

    def test_provider_roles_block_before_launch_after_ignored_edit_or_deletion(self):
        path = self.write("CLAUDE.local.md", "approved\n")
        self.approve()
        batch = {"checkpoint": {"tree": "tree"}}
        for change in (lambda: path.write_text("edited\n"), path.unlink):
            change()
            for role in ("plan", "fix", "review"):
                with self.subTest(role=role), patch.object(owner_git, "checkpoint", return_value={"tree": "tree"}), \
                     patch.object(owner_git, "admin") as admin, patch.object(owner_providers, "run") as launch:
                    with self.assertRaises(ApiError) as caught:
                        owner_providers.invoke(self.session, self.state, batch, role, {})
                    self.assertEqual(caught.exception.code, "trust_required")
                    admin.assert_not_called()
                    launch.assert_not_called()
            self.approve()

    def test_resume_rechecks_submitted_token_before_preflight(self):
        path = self.write("CLAUDE.local.md", "inspected\n")
        token = self.required()["trusted_snapshot"]
        batch = {"id": "batch", "attempts": [], "remote_head": "head", "baseline": "head",
                 "checkpoint": {"tree": "tree"}, "tasks": [], "settings": {}, "status": "paused"}
        self.state.update(batches=[batch], settings={})
        request = {"batch": "batch", "trusted_snapshot": token}
        with patch.object(owner_fixes, "operation", return_value=nullcontext()), \
             patch.object(owner_fixes, "refresh"), patch.object(owner_git, "prepare", return_value="head"), \
             patch.object(owner_git, "unstaged_index"), patch.object(owner_git, "admin", return_value="admin"), \
             patch.object(owner_git, "checkpoint", return_value={"tree": "tree"}), \
             patch.object(owner_providers, "preflight", side_effect=RuntimeError("reached preflight")) as preflight:
            path.write_text("changed before resume\n")
            with self.assertRaises(ApiError) as caught:
                owner_fixes.execute(self.session, self.state, request)
            self.assertEqual(caught.exception.code, "trust_required")
            self.assertNotIn("trusted_snapshot", self.state)
            preflight.assert_not_called()
            request["trusted_snapshot"] = caught.exception.details["trusted_snapshot"]
            with self.assertRaisesRegex(RuntimeError, "reached preflight"):
                owner_fixes.execute(self.session, self.state, request)
            self.assertEqual(self.state["trusted_snapshot"], request["trusted_snapshot"])
            self.assertIn("CLAUDE.local.md", self.state["trusted_configuration"])
            preflight.assert_called_once()


if __name__ == "__main__":
    unittest.main()
