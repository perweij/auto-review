"""Opt-in, recoverable commits and reply preparation after final verification."""
import os
import re
import uuid
from collections import deque

from .common import ApiError, now
from .events import emit
from .processes import run
from . import owner_commit, owner_commit_plan, owner_completion, owner_git, owner_publication


def timeout():
    value = os.environ.get("PR_REVIEW_COMMIT_TIMEOUT", "120")
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ApiError("invalid_request", "PR_REVIEW_COMMIT_TIMEOUT must be a positive integer number of seconds.")
    return int(value)


def check_context(state, batch):
    fingerprints = {i["key"]: i["fingerprint"] for i in state["feedback"] if not i.get("missing")}
    if (batch.get("discussion_stale") or batch.get("integrity_failure")
            or any(fingerprints.get(k) != v for k, v in batch["verified_fingerprints"].items())):
        raise ApiError("stale_discussion", "Verified feedback changed; inspect it before committing or preparing replies.")


def execute(session, state, batch, remote):
    """Caller owns execution_lock; verification remains ready on commit failure."""
    phase = batch.setdefault("commit_execution", {"status": "pending", "attempts": []})
    attempt = None
    try:
        deadline = timeout()
        if phase.get("inspection_required"):
            raise ApiError("commit_inspection", phase["error"])
        owner_git.validate(session, state)
        check_context(state, batch)
        worktree = state["worktree"]
        def git(*args):
            return session.repo.git(*args, cwd=worktree).stdout.strip()
        head = git("rev-parse", "HEAD")
        plan, progress = owner_commit_plan.update(session, batch, head)
        if remote != batch["remote_head"] and remote not in progress["commits"]:
            raise ApiError("stale_source", "GitHub head changed outside this batch's verified commits; inspect before resuming.")
        if progress["status"] in ("diverged", "ambiguous"):
            raise ApiError("stale_source", progress["reason"])
        for previous in phase["attempts"]:
            if previous["status"] in ("preparing", "running", "uncertain"):
                sha = progress["units"].get(previous["unit"])
                if sha and git("show", "-s", "--format=%T %P", sha) == previous["tree"] + " " + previous["parent"]:
                    if previous.get("worktree_tree"):
                        current = owner_git.checkpoint(session, state, "recover automatic commit")
                        if current["tree"] != previous["worktree_tree"] or git("write-tree") != git("rev-parse", "HEAD^{tree}"):
                            phase["inspection_required"] = True
                            raise ApiError("commit_changed", "Source or staging changed during the interrupted commit; inspect retained changes in Magit.")
                    previous.update(status="complete", commit=sha, recovered=True, finished_at=now())
                else:
                    previous.update(status="interrupted", finished_at=now())
        phase.update(status="running", started_at=now())
        phase.pop("error", None)

        def record_replies():
            owner_publication.bind_exact(batch, progress)
            owner_publication.draft_fixed(session, state, batch, automatic=True)
            session.save(state)

        record_replies()  # Repairs a crash between commit creation and drafting.
        while progress["completed"] < progress["total"]:
            unit = plan["units"][progress["completed"]]
            identity = uuid.uuid4().hex
            directory = session.path / "commits" / identity
            directory.mkdir(parents=True, mode=0o700)
            message = owner_commit.message(state, batch, unit)
            message_file = directory / "message.txt"
            message_file.write_text(message, encoding="utf-8")
            attempt = {"id": identity, "unit": unit["id"], "parent": head, "tree": unit["tree"],
                       "message": message, "artifacts": str(directory), "status": "preparing", "started_at": now()}
            phase["attempts"].append(attempt)
            session.save(state)
            prepared = owner_commit.prepare(session, state, {"batch": batch["id"], "unit": unit["id"]})
            if not prepared["ready"]:
                attempt["status"] = "blocked"
                raise ApiError("commit_blocked", prepared["reason"])
            before = owner_git.checkpoint(session, state, "before automatic commit")
            owner_git.validate(session, state)
            if git("rev-parse", "HEAD") != head or git("write-tree") != unit["tree"]:
                raise ApiError("commit_changed", "HEAD or staged changes changed before the commit; inspect in Magit.")
            attempt.update(status="running", worktree_tree=before["tree"])
            session.save(state)
            emit("progress", message=f"Owner auto commit {progress['completed'] + 1}/{progress['total']}: {message.splitlines()[0]}")
            output = deque(maxlen=20)
            def line(stream, text):
                output.append(text)
                emit("progress", message=text, stream=stream)
            # Unlike the read/snapshot Git helper, this deliberately honors hooks
            # and signing. No shell, editor, --all, --no-verify or unsigned retry.
            result = run(["git", "commit", "--file", str(message_file), "--no-edit"], cwd=worktree,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "false"},
                         timeout=deadline, stage="owner-commit", on_line=line,
                         stdout_path=directory / "stdout.log", stderr_path=directory / "stderr.log")
            attempt.update(exit_code=result.returncode, finished_at=now())
            new_head = git("rev-parse", "HEAD")
            owner_git.validate(session, state)
            after = owner_git.checkpoint(session, state, "after automatic commit")
            exact = (new_head != head and git("show", "-s", "--format=%T %P", new_head) == unit["tree"] + " " + head)
            if new_head != head and not exact:
                phase["inspection_required"] = True
                attempt.update(status="unexpected", observed_commit=new_head)
                raise ApiError("commit_changed", "Git created an unexpected commit. Existing commits and edits were retained; inspect in Magit.")
            if exact:
                attempt.update(status="complete", commit=new_head)
                head = new_head
                plan, progress = owner_commit_plan.update(session, batch, head)
            if after["tree"] != before["tree"] or (exact and git("write-tree") != unit["tree"]):
                phase["inspection_required"] = True
                raise ApiError("commit_changed", "A commit hook or another writer changed source or staging; inspect retained changes in Magit.")
            if exact:
                record_replies()
                emit("progress", message=f"Owner commit confirmed: {new_head} ({progress['completed']}/{progress['total']})")
            if result.returncode or not exact:
                if not exact:
                    attempt["status"] = "failed"
                detail = "Commit timed out; inspect hooks/signing and retry with r." if result.reason == "timeout" else (
                    (result.stderr or result.stdout or "\n".join(output)).strip()[-1500:] or "Git did not create the expected commit.")
                raise ApiError("commit_failed", detail)
            attempt = None
        issues = batch.get("reply_issues", [])
        phase.update(status="paused" if issues else "complete", finished_at=now())
        if issues:
            phase["error"] = "Replies need inspection: " + "; ".join(i["reason"] for i in issues)
        session.save(state)
    except BaseException as exc:
        phase.update(status="paused", error=str(exc), finished_at=now())
        if attempt and attempt["status"] in ("running", "preparing"):
            attempt.update(status="uncertain", error=str(exc))
        session.save(state)
        emit("progress", message="Fix verified; automatic completion paused: " + str(exc))
        if not isinstance(exc, ApiError) or exc.code == "cancelled":
            raise
    progress = batch.get("commit_progress", {})
    replies = [r for r in state["replies"] if r.get("batch") == batch["id"] and r["status"] == "staged"]
    completion = "automatic commits complete" if phase["status"] == "complete" else f"automatic completion {phase['status']}"
    emit("progress", message=f"Fix verified · {completion} · "
         f"{progress.get('completed', 0)}/{progress.get('total', 0)} fixes committed · {len(replies)} replies staged" +
         (" (awaiting push). Push in Magit, then P previews and confirms publication."
          if any(r.get("awaiting_push") for r in replies) else
          ". P previews and confirms publication." if replies else ""))
    return owner_completion.reconcile(session, state)
