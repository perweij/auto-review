"""Prepare an exact verified fix for human committing, without touching source."""
import os
from pathlib import Path
import stat
import tempfile

from .common import ApiError, now
from .processes import run
from . import owner_git as workspace, owner_commit_plan


def message(state, batch, unit=None):
    items = {i["key"]: i for i in state["feedback"]}
    def line(text):
        return " ".join(str(text or "").split())
    titles = []
    for key in unit["items"] if unit else batch["verified_items"]:
        item = items[key]
        title = item.get("title") or (item.get("comments") or [{}])[0].get("body") or "Review feedback"
        titles.append(line(title.splitlines()[0])[:160])
    subject = ("Fix: " + titles[0])[:72] if len(titles) == 1 else f"Address review feedback for PR #{state['pr']}"
    if unit and len(batch.get("commit_plan", {}).get("units", [])) > 1 and titles:
        subject = ("Fix: " + titles[0])[:72]
    summaries = list(dict.fromkeys(line(t.get("implementation", {}).get("summary")) for t in batch["tasks"]
                                  if t.get("status") == "complete" and (not unit or t["id"] in unit["tasks"])))
    body = "\n".join("- " + title for title in titles)
    return subject + "\n\n" + body + "\n\n" + "\n".join(s for s in summaries if s) + "\n\nPR: " + state["url"] + "\n"


def unverified_reason(batch):
    tasks = batch.get("tasks", [])
    completed = sum(task.get("status") == "complete" for task in tasks)
    pending = next((task for task in tasks if task.get("status") not in ("complete", "skipped", "blocked")), {})
    report = pending.get("review") or pending.get("implementation") or batch.get("review") or {}
    reason = batch.get("error") or "; ".join(report.get("blocking", [])) or report.get("summary") or "Combined verification has not passed."
    return (f"Selected fix is {batch['status']} ({completed}/{len(tasks)} tasks complete). {reason} "
            "Changes remain unstaged. Inspect this batch under Fixes and resume with r before preparing a commit.")


def prepare(session, state, request):
    workspace.validate(session, state)
    workspace.configure_push(session, state)
    result = {"ready": False, "worktree": state["worktree"]}
    def stopped(reason):
        return {**result, "reason": reason, "owner": session.view(state)}
    head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
    batches = [b for b in state["batches"] if b["id"] == request["batch"]] if request.get("batch") else state["batches"][-1:]
    if not batches:
        return stopped("No saved fix matches this selection; choose a fix under Fixes or inspect the worktree in Magit.")
    batch = batches[0]
    if batch["status"] != "ready":
        return stopped(unverified_reason(batch))
    plan, progress = owner_commit_plan.update(session, batch, head)
    session.save(state)
    selected = [u for u in plan["units"] if (request.get("unit") == u["id"] or
                request.get("item") in u["items"] or request.get("task") in u["tasks"])]
    explicit = any(request.get(k) for k in ("unit", "item", "task"))
    if len(selected) > 1:
        return stopped("Choose one logical fix or select its batch for the next commit.")
    if explicit and not selected:
        return stopped("This selection has no separate code commit; inspect the fix plan or select its batch.")
    if progress["complete"] or (selected and selected[0]["id"] in progress["units"]):
        commits = [progress["units"][selected[0]["id"]]] if selected else progress["commits"]
        return {**stopped("Saved fix already committed in " + ", ".join(sha[:12] for sha in commits) +
                         "; opening Magit for inspection."), "committed": True, "commits": commits}
    if progress["status"] in ("diverged", "ambiguous"):
        associated = list(dict.fromkeys(sha for association in batch.get("associations", {}).values()
                                        for sha in association["commits"] if workspace.ancestor(session, sha, head)))
        if associated:
            return stopped("Associated commits: " + ", ".join(sha[:12] for sha in associated) +
                           ". HEAD differs from the verified baseline; opening Magit for inspection.")
        return stopped(progress["reason"])
    fingerprints = {i["key"]: i["fingerprint"] for i in state["feedback"] if not i.get("missing")}
    if (batch.get("discussion_stale") or batch.get("integrity_failure")
            or any(fingerprints.get(k) != v for k, v in batch.get("verified_fingerprints", {}).items())):
        return stopped("The verified fix context changed; inspect and reverify before preparing a commit.")
    if not plan["units"]:
        return stopped("The verified fix has no code changes to commit.")
    unit = plan["units"][progress["completed"]]
    if selected and selected[0]["id"] != unit["id"]:
        return stopped(f"Commit fix {progress['completed'] + 1} of {progress['total']} first; select the batch and press m to prepare it.")
    target = unit["tree"]
    paths = session.repo.git("diff", "--name-only", "--no-renames", "-z", head, target, "--").stdout.split("\0")[:-1]
    if not paths:
        return stopped("The verified fix has no code changes to commit.")
    if any("\ufffd" in path for path in paths):
        return stopped("This fix contains non-UTF-8 paths; stage it manually in Magit.")
    # A merge/cherry-pick/rebase would change the semantics of an ordinary commit.
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = session.repo.git("rev-parse", "--path-format=absolute", "--git-path", name, cwd=state["worktree"]).stdout.strip()
        if Path(path).exists():
            return stopped("A Git operation is in progress; finish it in Magit before preparing a fix commit.")
    index = Path(session.repo.git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=state["worktree"]).stdout.strip())
    lock = index.with_name(index.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return stopped("The Git index is busy; retry when the other Git operation finishes.")
    installed = False
    try:
        with os.fdopen(descriptor, "wb") as output, tempfile.TemporaryDirectory(prefix="commit-index-", dir=session.path) as temporary:
            original = index.read_bytes() if index.exists() else None
            temporary_index = Path(temporary) / "index"
            if original is not None:
                temporary_index.write_bytes(original)
            env = {**os.environ, "GIT_INDEX_FILE": str(temporary_index), "GIT_OPTIONAL_LOCKS": "0"}
            def git(*args, input_bytes=b""):
                value = run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=state["worktree"], env=env, input_bytes=input_bytes)
                if value.returncode:
                    raise ApiError("commit_preparation_failed", value.stderr or "Cannot prepare the verified fix index.")
                return value.stdout
            if original is None:
                git("read-tree", head)
            if git("ls-files", "--unmerged", "-z"):
                return stopped("Resolve index conflicts in Magit before preparing a fix commit.")
            def tree_entries(tree):
                entries = {}
                for record in git("ls-tree", "-r", "-z", tree).split("\0"):
                    if record:
                        info, name = record.split("\t", 1)
                        mode, _kind, oid = info.split()
                        entries[name] = (mode, oid)
                return entries
            final = tree_entries(target)
            staged = git("diff", "--cached", "--name-only", "--no-renames", "-z", head, "--").split("\0")[:-1]
            current = tree_entries(git("write-tree").strip())
            if any(p not in paths or current.get(p) != final.get(p) for p in staged):
                return stopped("Other changes are already staged; inspect or unstage them in Magit before preparing this fix.")
            # Install saved blobs, not working files: later edits, even in the same
            # file, remain unstaged. NUL-delimited index-info handles literal paths.
            zero = "0" * len(head)
            records = []
            for path in paths:
                mode, oid = final.get(path, ("0", zero))
                records.append(f"{mode} {oid}\t{path}\0")
            git("update-index", "--add", "--remove", "-z", "--index-info", input_bytes="".join(records).encode())
            if git("write-tree").strip() != target:
                raise ApiError("commit_preparation_failed", "Prepared index differs from the verified fix; index left unchanged.")
            if session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip() != head:
                return stopped("HEAD changed while preparing the commit; retry after inspecting Magit.")
            if (index.read_bytes() if index.exists() else None) != original:
                return stopped("The Git index changed while preparing the commit; inspect it in Magit.")
            os.fchmod(output.fileno(), stat.S_IMODE(index.stat().st_mode) if index.exists() else 0o644)
            output.write(temporary_index.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.replace(lock, index)
        installed = True
    finally:
        if not installed:
            lock.unlink(missing_ok=True)
    batch["commit_preparation"] = {"at": now(), "head": head, "tree": target, "paths": paths, "unit": unit["id"]}
    session.save(state)
    return {**result, "ready": True, "owner": session.view(state), "batch": batch["id"], "paths": paths,
            "unit": unit["id"], "position": progress["completed"] + 1, "total": progress["total"],
            "message": message(state, batch, unit)}
