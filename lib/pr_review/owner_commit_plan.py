"""Verified commit boundaries and exact commit-series recognition."""
from . import owner_git


def build(session, batch):
    """Use reviewed task checkpoints only when they form an unbroken chain."""
    baseline = session.repo.git("rev-parse", batch["baseline"] + "^{tree}").stdout.strip()
    final = batch["final_tree"]
    keys = batch.get("verified_items", [])
    fixed = {o["item"] for o in batch.get("review", {}).get("outcomes", []) if o["status"] == "fixed"}
    plan = {"baseline": batch["baseline"], "final_tree": final, "units": [], "mode": "tasks"}

    def combined(reason):
        return {**plan, "mode": "combined", "reason": reason,
                "units": [{"id": "combined", "before_tree": baseline, "tree": final,
                           "items": keys, "tasks": [t["id"] for t in batch["tasks"] if t["status"] == "complete"]}]
                if baseline != final else []}

    # Commit execution is a journaled transaction. Keep an older combined
    # transaction stable after preparation has started; never rewrite history.
    saved = batch.get("commit_plan", {})
    if (saved.get("mode") == "combined" and saved.get("baseline") == batch["baseline"]
            and saved.get("final_tree") == final and batch.get("commit_execution", {}).get("attempts")):
        return combined(saved.get("reason", "Retain the started combined commit transaction."))

    previous, units = baseline, []
    for task in batch["tasks"]:
        if task["status"] != "complete":
            continue
        review = task.get("review", {})
        before, after = task.get("before", {}).get("tree"), task.get("after", {}).get("tree")
        items = [key for key in task["items"] if key in keys]
        assessed = {o["item"] for o in review.get("outcomes", []) if o["status"] == "fixed"}
        if (before != previous or not after or review.get("complete") is not True or review.get("blocking")
                or not (set(items) & fixed) <= assessed):
            return combined("Task checkpoints no longer provide separate verified boundaries; commit the combined result.")
        if after != before:
            units.append({"id": task["id"], "before_tree": before, "tree": after, "items": items, "tasks": [task["id"]]})
        elif set(items) & fixed:
            # Earlier changes satisfied another concern. Do not invent an empty
            # commit or attribute the outcome to an arbitrary filename/task.
            if not units:
                return combined("A fixed outcome has no separate patch; retain its combined verification.")
            covered = {key for unit in units for key in unit["items"]} | set(items)
            units = [{"id": units[0]["id"], "before_tree": baseline, "tree": after,
                      "items": [key for key in keys if key in covered],
                      "tasks": [identity for unit in units for identity in unit["tasks"]] + [task["id"]]}]
        previous = after
    if previous != final and units:
        # Task snapshots remain useful boundaries even if the final review
        # required additional changes. Only the final tree is the full fix.
        units.append({"id": "integration-repair", "kind": "integration_repair",
                      "before_tree": previous, "tree": final, "items": keys, "tasks": []})
        plan["reason"] = "Preserve task commits and append the integration repair; publish only the complete verified result."
    if not units and baseline != final:
        return combined("Separate reviewed task checkpoints are unavailable; commit the combined result.")
    plan["units"] = units
    return plan


def inspect(session, batch, head, plan=None):
    """Match full trees and sole parents; never infer ownership from paths."""
    plan = plan or build(session, batch)
    units = plan["units"]
    records = owner_git.commit_records(session, batch["baseline"], head)
    bulk = [sha for sha, tree, parents in records if tree == batch["final_tree"] and parents == [batch["baseline"]]]
    result = {"total": len(units), "completed": 0, "commits": [], "units": {}, "items": {}, "complete": False}
    if len(bulk) > 1:
        return {**result, "status": "ambiguous", "reason": "More than one exact fix commit matches; associate explicitly."}
    if bulk:
        sha = bulk[0]
        return {**result, "status": "complete", "complete": True, "method": "exact",
                "completed": len(units), "commits": bulk, "units": {unit["id"]: sha for unit in units},
                "items": {key: bulk for key in batch.get("verified_items", [])}}
    paths = [[]]
    for unit in units:
        extended = [path + [sha] for path in paths for sha, tree, parents in records
                    if tree == unit["tree"] and parents == [path[-1] if path else batch["baseline"]]]
        if not extended:
            break
        if len(extended) > 1:
            return {**result, "status": "ambiguous", "reason": "More than one exact commit sequence matches; associate explicitly."}
        paths = extended
    commits = paths[0]
    completed = len(commits)
    by_unit = {unit["id"]: sha for unit, sha in zip(units, commits)}
    by_item = {}
    for unit, sha in zip(units, commits):
        for key in unit["items"]:
            by_item.setdefault(key, []).append(sha)
    # A task snapshot alone does not establish a fixed outcome when a later
    # integration repair is required. Withhold associations until it exists.
    if any(u.get("kind") == "integration_repair" for u in units) and completed < len(units):
        by_item = {}
    result.update(completed=completed, commits=commits, units=by_unit, items=by_item, method="tasks")
    if units and completed == len(units):
        result.update(status="complete", complete=True)
    elif head == (commits[-1] if commits else batch["baseline"]):
        result.update(status="partial" if commits else "unstarted")
    else:
        result.update(status="diverged", reason="HEAD changed outside the verified commit sequence; inspect or associate the existing commits in Owner.")
    return result


def update(session, batch, head):
    """Retain deterministic plan/progress for the GUI and subsequent handoffs."""
    plan = build(session, batch)
    progress = inspect(session, batch, head, plan)
    batch.update(commit_plan=plan, commit_progress=progress)
    batch.pop("commit_match", None)
    if progress["complete"]:
        batch["commit_match"] = {"commit": progress["commits"][-1]}
    return plan, progress
