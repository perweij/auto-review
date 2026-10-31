"""Reconcile verified fixes with local code, without Git or GitHub writes."""
import copy

from .common import ApiError
from . import owner_git, owner_commit_plan


def execution_complete(batch, *, current_discussion=True):
    """Stale completed execution needs a new batch, never resumed committing."""
    if batch.get('status') != 'ready' or not batch.get('review', {}).get('complete') or batch.get('review', {}).get('blocking'):
        return False
    execution = batch.get('commit_execution', {})
    if (batch.get('integrity_failure') or (current_discussion and batch.get('discussion_stale')) or execution.get('inspection_required')
            or (execution and execution.get('status') != 'complete')):
        return False
    progress = batch.get('commit_progress', {})
    return progress.get('complete', False) or not batch.get('commit_plan', {}).get('units', [1])


def manual_triage(state, item):
    """Keep an explicit triage decision from being undone by the next refresh."""
    item.pop("local_completion", None)
    latest = next((b for b in reversed(state["batches"]) if item["key"] in b["items"]), None)
    if latest:
        item["completion_override"] = {"batch": latest["id"], "fingerprint": item["fingerprint"]}


def reconcile(session, state):
    """Called under the execution lock; only proven local findings are finished."""
    result = {"status": "complete", "committed_batches": 0, "handled_items": [], "reopened_items": []}
    def view(value):
        return {**session.view(value), "reconciliation": result}
    if not state.get("worktree"):
        return view(state)
    updated = copy.deepcopy(state)
    try:
        owner_git.validate(session, state)
        head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
        unchanged = None
        latest = {}
        for batch in updated["batches"]:
            for key in batch["items"]:
                latest[key] = batch
            batch.pop("commit_match", None)
            batch.pop("commit_progress", None)
            if batch["status"] != "ready" or not batch.get("final_tree"):
                continue
            _plan, progress = owner_commit_plan.update(session, batch, head)
            if progress["complete"]:
                result["committed_batches"] += 1

        for item in updated["feedback"]:
            batch = latest.get(item["key"], {})
            fingerprint = item["fingerprint"]
            commits = batch.get("commit_progress", {}).get("items", {}).get(item["key"])
            proof = None
            if (item["kind"] in ("local_review", "issue_plan") and not item.get("missing")
                    and batch.get("status") == "ready" and commits
                    and not batch.get("discussion_stale") and not batch.get("integrity_failure")
                    and not batch.get("commit_execution", {}).get("inspection_required")
                    and batch.get("verified_fingerprints", {}).get(item["key"]) == fingerprint
                    and item["key"] in batch.get("verified_items", [])
                    and batch.get("review", {}).get("complete") is True
                    and not batch["review"].get("blocking")
                    and any(o["item"] == item["key"] and o["status"] == "fixed"
                            for o in batch["review"]["outcomes"])):
                proof = {"batch": batch["id"], "commit": commits[-1], "fingerprint": fingerprint}
            # No commit can witness an already-satisfied finding. Bind that
            # verification to its exact code/base and require a clean index and
            # worktree, so later edits or a new verification attempt reopen it.
            if (item["kind"] in ("local_review", "issue_plan") and not item.get("missing")
                    and execution_complete(batch) and batch.get("commit_plan", {}).get("units") == []
                    and batch.get("baseline") == head
                    and batch.get("verified_base")
                    and batch["verified_base"] == state["metadata"]["baseRefOid"]
                    and batch.get("verified_fingerprints", {}).get(item["key"]) == fingerprint
                    and item["key"] in batch.get("verified_items", [])
                    and any(o["item"] == item["key"] and o["status"] in ("already_addressed", "no_change")
                            for o in batch["review"]["outcomes"])):
                if unchanged is None:
                    tree = session.repo.git("rev-parse", "HEAD^{tree}", cwd=state["worktree"]).stdout.strip()
                    dirty = session.repo.git("status", "--porcelain=v1", "--untracked-files=all",
                                             "--ignore-submodules=none", cwd=state["worktree"]).stdout
                    unchanged = (tree, not dirty)
                if unchanged == (batch.get("final_tree"), True):
                    proof = {"batch": batch["id"], "head": head, "tree": unchanged[0],
                             "base": batch["verified_base"], "fingerprint": fingerprint}
            previous = item.get("local_completion")
            if previous and not proof:
                item.pop("local_completion")
                if item.get("triage") == "handled":
                    item.update(triage="attention", attention=True, queued=True)
                    result["reopened_items"].append(item["key"])
            override = item.get("completion_override")
            if (proof and item.get("triage") == "attention"
                    and override != {"batch": proof["batch"], "fingerprint": fingerprint}):
                item.update(triage="handled", attention=False, queued=False, local_completion=proof)
                result["handled_items"].append(item["key"])
            elif proof and item.get("local_completion"):
                item["local_completion"] = proof
        # A concurrent Magit commit/rewrite must not install stale conclusions.
        if session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip() != head:
            raise ApiError("head_changed", "HEAD changed during the commit check; refresh again.")
    except ApiError as exc:
        result = {"status": "unavailable", "reason": str(exc)}
        return view(state)
    if updated != state:
        session.save(updated)
    return view(updated)
