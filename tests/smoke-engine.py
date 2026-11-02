#!/usr/bin/env python3
"""Opt-in paid CLI smoke: final-verify a disposable local candidate with the shared review engine.

Never contacts GitHub. One slot matching synthesis uses the direct path (no
bubblewrap); ``--second-provider`` adds an independent sandboxed reviewer, which needs
bubblewrap and a container where providers can start inside it.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review import acceptance, owner_git
from pr_review.common import locked
from pr_review.owner_fixes import successful, validate_result
from pr_review.owner_providers import preflight
from pr_review.owner_state import OwnerSession, open_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["claude", "codex"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--second-provider", choices=["claude", "codex"])
    options = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="pr-engine-live-smoke-"))
    print("Smoke artifacts: " + str(directory), flush=True)
    repo = directory / "repo"
    repo.mkdir()
    os.environ.update(XDG_STATE_HOME=str(directory / "state"), GIT_CONFIG_GLOBAL=str(directory / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Engine smoke", GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="Engine smoke", GIT_COMMITTER_EMAIL="smoke@example.invalid")

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "example.py").write_text("def answer():\n    return 1\n")
    git("add", ".")
    git("commit", "-qm", "Synthetic baseline")
    head = git("rev-parse", "HEAD")
    git("remote", "add", "origin", "https://github.com/example/engine-smoke.git")
    worktree = directory / "owner-worktree"
    git("worktree", "add", "-b", "pr-owner-smoke", str(worktree), head)
    view = open_session({"cwd": str(repo), "repository": "github.com/example/engine-smoke", "pr": 1})
    session = OwnerSession({"session": view["session"]})
    state = session.load()
    choice = {"provider": options.provider, "model": options.model, "effort": options.effort}
    slots = [{"id": "reviewer-1", **choice}]
    if options.second_provider:
        slots.append({"id": "reviewer-2", "provider": options.second_provider, "model": options.model, "effort": options.effort})
    state.update(worktree=str(worktree), branch="pr-owner-smoke", metadata={"headRefOid": head, "baseRefOid": head},
                 settings={"fix": dict(choice), "review": dict(choice), "verification": "focused",
                           "final_review": {"reviewer_slots": slots, "synthesis": dict(choice), "synthesis_tests": "focused"}})
    keys = ["thread:smoke"]
    feedback = [{"key": keys[0], "kind": "thread", "comments": [{"author": "reviewer", "body":
        "answer() in example.py must return 2. Check it with a focused Python assertion."}]}]
    with locked(session.execution_lock):
        session.save(state)
        preflight(session, state)
        # An uncommitted candidate that satisfies the feedback: a modification and an untracked file.
        (worktree / "example.py").write_text("def answer():\n    return 2\n")
        (worktree / "test_example.py").write_text("from example import answer\n\n\ndef test_answer():\n    assert answer() == 2\n")
        batch = {"id": "live-smoke", "settings": state["settings"], "baseline": head, "baseline_ref": owner_git.pin_commit(session, head),
                 "attempts": [], "items": keys, "tasks": [], "status": "reviewing", "review_engine": 1,
                 "checkpoint": owner_git.checkpoint(session, state, "candidate"), "git_admin": owner_git.admin(session, state)}
        state["batches"].append(batch)
        session.save(state)
        initial = owner_git.admin(session, state)
        data = {"feedback": feedback, "verification_cases": [], "completed_tasks": [], "scope": "Review the candidate.",
                "approved_issue_plan": None}
        result, review = acceptance.review_owner(session, state, batch, keys, data)
        review = validate_result(review, keys)
        print(json.dumps({"assessment": result.value["assessment"], "summary": result.value["summary"],
                          "findings": [(f["id"], f["blocking"], f["title"]) for f in result.value["findings"]],
                          "artifact": str(result.artifact)}, indent=2))
        if not successful(review) or initial != owner_git.admin(session, state):
            raise RuntimeError("The engine did not pass the synthetic candidate or modified Git administrative state")
        if owner_git.checkpoint(session, state, "after")["tree"] != batch["checkpoint"]["tree"]:
            raise RuntimeError("The review changed the live candidate")
    print(json.dumps({"provider": options.provider, "model": options.model, "status": "passed", "artifacts": str(directory)}))


if __name__ == "__main__":
    main()
