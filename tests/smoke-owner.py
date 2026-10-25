#!/usr/bin/env python3
"""Opt-in paid CLI smoke: plan/fix/review a disposable fixture, never contact GitHub."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review.common import locked
from pr_review.owner_state import open_session, OwnerSession
from pr_review.owner_providers import preflight, invoke
from pr_review.owner_fixes import validate_plan, validate_result, successful
from pr_review import owner_git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["claude", "codex"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="low")
    options = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="pr-owner-live-smoke-"))
    print("Smoke artifacts: " + str(directory), flush=True)
    repo = directory / "repo"
    repo.mkdir()
    os.environ.update(XDG_STATE_HOME=str(directory / "state"), GIT_CONFIG_GLOBAL=str(directory / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Owner smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="Owner smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid")
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "example.py").write_text("def answer():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "Synthetic baseline")
    head = git("rev-parse", "HEAD")
    git("remote", "add", "origin", "https://github.com/example/owner-smoke.git")
    worktree = directory / "owner-worktree"
    git("worktree", "add", "-b", "pr-owner-smoke", str(worktree), head)
    view = open_session({"cwd": str(repo), "repository": "github.com/example/owner-smoke", "pr": 1})
    session = OwnerSession({"session": view["session"]})
    state = session.load()
    choice = {"provider": options.provider, "model": options.model, "effort": options.effort}
    state.update(worktree=str(worktree), branch="pr-owner-smoke", metadata={"headRefOid": head, "baseRefOid": head},
                 settings={"fix": dict(choice), "review": dict(choice), "verification": "focused"})
    with locked(session.execution_lock):
        session.save(state)
        preflight(session, state)
        batch = {"id": "live-smoke", "settings": state["settings"], "baseline": head, "attempts": [],
                 "checkpoint": owner_git.checkpoint(session, state, "before")}
        state["batches"].append(batch)
        session.save(state)
        initial = owner_git.admin(session, state)
        context = {"feedback": [{"key": "thread:smoke", "comments": [{"author": "reviewer", "body":
            "The answer() function in example.py must return 2. Update just that return value and check it with a focused Python assertion."}]}]}
        plan = validate_plan(invoke(session, state, batch, "plan", context), context["feedback"])
        context["task"] = plan[0]
        fixed = validate_result(invoke(session, state, batch, "fix", context), ["thread:smoke"])
        if not successful(fixed) or "return 2" not in (worktree / "example.py").read_text():
            raise RuntimeError("Live fixer did not implement and verify the synthetic change")
        context.update(implementation_report=fixed,
                       task_diff=owner_git.diff(session, head, batch["checkpoint"]["tree"]))
        reviewed = validate_result(invoke(session, state, batch, "review", context), ["thread:smoke"])
        if not successful(reviewed) or initial != owner_git.admin(session, state):
            raise RuntimeError("Live review failed or modified Git administrative state")
    print(json.dumps({"provider": options.provider, "model": options.model, "status": "passed", "artifacts": str(directory)}))


if __name__ == "__main__":
    main()
