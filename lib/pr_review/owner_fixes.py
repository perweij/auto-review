"""Dependency-aware owner batches with explicit, recoverable task transitions."""
import copy
import re
import uuid

from .common import ApiError, now
from .events import emit
from .operations import operation
from . import owner_git as workspace, owner_providers as models
from .owner_state import refresh


def validate_plan(value, items):
    tasks = value.get("tasks") if isinstance(value, dict) else None
    keys = [i["key"] for i in items]
    if not isinstance(tasks, list) or not tasks:
        raise ApiError("invalid_plan", "Planner returned no tasks.")
    found, covered = {}, []
    for task in tasks:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task["id"]) or task["id"] in found
                or not isinstance(task.get("items"), list) or not task["items"]
                or any(not isinstance(k, str) for k in task["items"])
                or not isinstance(task.get("depends_on"), list)
                or any(not isinstance(k, str) for k in task["depends_on"])
                or len(set(task["depends_on"])) != len(task["depends_on"])
                or not isinstance(task.get("rationale"), str) or not task["rationale"].strip()
                or not isinstance(task.get("expected"), list)
                or any(not isinstance(e, dict) or not isinstance(e.get("item"), str)
                       or not isinstance(e.get("outcome"), str) or not e["outcome"].strip() for e in task["expected"])):
            raise ApiError("invalid_plan", "Invalid or duplicate task in owner plan.")
        if sorted(e["item"] for e in task["expected"]) != sorted(task["items"]):
            raise ApiError("invalid_plan", "Each task requires an expected outcome for every original item exactly once.")
        found[task["id"]] = copy.deepcopy(task)
        covered.extend(task["items"])
    if sorted(covered) != sorted(keys):
        raise ApiError("invalid_plan", "Plan must cover each selected feedback item exactly once.")
    if any(dep not in found for task in tasks for dep in task["depends_on"]):
        raise ApiError("invalid_plan", "Plan refers to an unknown prerequisite.")
    ordered, done = [], set()
    while len(done) < len(found):
        ready = [t for t in found.values() if t["id"] not in done and set(t["depends_on"]) <= done]
        if not ready:
            raise ApiError("invalid_plan", "Task dependencies contain a cycle.")
        task = min(ready, key=lambda t: min(keys.index(k) for k in t["items"]))
        task.update(status="queued", repairs=0)
        ordered.append(task)
        done.add(task["id"])
    return ordered


def validate_result(value, keys):
    if (not isinstance(value, dict) or not isinstance(value.get("summary"), str)
            or type(value.get("complete")) is not bool
            or any(not isinstance(value.get(k), list) or any(not isinstance(x, str) for x in value[k]) for k in ("checks", "blocking"))
            or not isinstance(value.get("outcomes"), list)):
        raise ApiError("invalid_result", "Incomplete owner verification result.")
    outcomes = value["outcomes"]
    if any(not isinstance(o, dict) or not isinstance(o.get("item"), str)
           or o.get("status") not in ("fixed", "already_addressed", "clarify", "no_change")
           or not isinstance(o.get("evidence"), str) or not o["evidence"].strip() for o in outcomes):
        raise ApiError("invalid_result", "Every feedback outcome requires a status and evidence.")
    if sorted(o["item"] for o in outcomes) != sorted(keys):
        raise ApiError("invalid_result", "Verification must assess each selected item exactly once.")
    return value


def successful(value):
    return value["complete"] and not value["blocking"] and all(o["status"] != "clarify" for o in value["outcomes"])


def implementation_ready(session, state, task):
    report = task["implementation"]
    if not report["complete"] or any(o["status"] == "clarify" for o in report["outcomes"]):
        task["status"] = "needs_attention"
        session.save(state)
        return False
    return True


def context(session, state, batch, keys, before):
    return {"feedback": session.items(state, keys), "baseline": batch["baseline"],
            "batch_diff": workspace.diff(session, batch["baseline"], batch["checkpoint"]["tree"]),
            "task_diff": workspace.diff(session, before, batch["checkpoint"]["tree"]),
            "completed_tasks": [t for t in batch["tasks"] if t["status"] == "complete"]}


def run_task(session, state, batch, task):
    if batch.get("verify_only") and task["status"] != "reviewing":
        task.update(before=batch["checkpoint"], status="reviewing", implementation={"summary": "Read-only verification requested"})
    if task["status"] != "reviewing":
        task["before"] = batch["checkpoint"]
        task["status"] = "fixing"
        session.save(state)
        data = context(session, state, batch, task["items"], task["before"]["tree"])
        data["task"] = {k: task[k] for k in ("id", "rationale", "expected")}
        task["implementation"] = validate_result(models.invoke(session, state, batch, "fix", data), task["items"])
        if not implementation_ready(session, state, task):
            return False
        task["status"] = "reviewing"
        session.save(state)
    while True:
        data = context(session, state, batch, task["items"], task["before"]["tree"])
        data["implementation_report"] = task["implementation"]
        review = validate_result(models.invoke(session, state, batch, "review", data), task["items"])
        task["review"] = review
        task["after"] = batch["checkpoint"]
        session.save(state)
        if successful(review):
            task["status"] = "complete"
            session.save(state)
            return True
        if not batch.get("verify_only") and review["complete"] and review["blocking"] and task["repairs"] < 1:
            task["repairs"] += 1  # Persist before launching; crashes do not grant extra repairs.
            task["status"] = "repairing"
            session.save(state)
            data["review_findings"] = review
            task["implementation"] = validate_result(models.invoke(session, state, batch, "fix", data), task["items"])
            if not implementation_ready(session, state, task):
                return False
            task["status"] = "reviewing"
            session.save(state)
            continue
        task["status"] = "needs_attention"
        session.save(state)
        return False


def final_review(session, state, batch):
    keys = [k for t in batch["tasks"] if t["status"] == "complete" for k in t["items"]]
    if not keys:
        batch["status"] = "paused"
        return
    while True:
        batch["status"] = "reviewing"
        session.save(state)
        data = context(session, state, batch, keys, batch["baseline"])
        data["scope"] = "Review the combined diff and all completed outcomes, including interactions between tasks."
        review = validate_result(models.invoke(session, state, batch, "review", data), keys)
        batch["review"] = review
        if successful(review):
            batch.update(status="ready", final_tree=batch["checkpoint"]["tree"], completed_at=now(),
                         verified_items=keys, verified_fingerprints={i["key"]: i["fingerprint"] for i in session.items(state, keys)})
            for task in batch["tasks"]:
                if task.get("after") and task["after"]["tree"] != batch["final_tree"]:
                    task["verification_superseded"] = True
            return
        if not batch.get("verify_only") and review["complete"] and review["blocking"] and batch["repairs"] < 1:
            batch["repairs"] += 1
            session.save(state)
            data["review_findings"] = review
            result = validate_result(models.invoke(session, state, batch, "fix", data), keys)
            if not result["complete"] or any(o["status"] == "clarify" for o in result["outcomes"]):
                batch["status"] = "paused"
                return
            continue
        batch["status"] = "paused"
        return


def check_fresh(session, state, batch):
    refresh(session, state)
    if state["metadata"]["headRefOid"] != batch["remote_head"]:
        raise ApiError("stale_source", "GitHub head changed during the batch; reconcile in Magit before continuing.")
    if batch.get("discussion_stale"):
        raise ApiError("stale_discussion", "Selected feedback changed during the batch; inspect and replan unfinished work.")


def execute(session, state, request):
    """Caller holds only the owner execution lock throughout inference."""
    with operation({"common": str(session.repo.common), "pr": str(session.repo.pr), "kind": "owner-fix"}):
        batch = None
        try:
            # Refresh before creating a worktree; failure leaves existing source untouched.
            refresh(session, state)
            if request.get("trusted_snapshot"):
                state["trusted_snapshot"] = request["trusted_snapshot"]
            remote = workspace.prepare(session, state)
            if request.get("batch"):
                batch = next((b for b in state["batches"] if b["id"] == request["batch"]), None)
                if not batch:
                    raise ApiError("invalid_request", "Choose an existing owner batch.")
                orphaned = [a for a in batch["attempts"] if a["status"] == "running"]
                for attempt in orphaned:
                    attempt.update(status="interrupted", finished_at=now(), error="Owning operation ended before recording completion.")
                if batch["remote_head"] != remote or session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip() != batch["baseline"]:
                    # A human commit ends execution of this batch, but its accepted
                    # tree remains eligible for subsequent commit association.
                    if batch["status"] == "ready":
                        batch = None
                    raise ApiError("stale_source", "HEAD changed; reconcile in Magit and start a new batch.")
                workspace.unstaged_index(session, state)
                identity = workspace.admin(session, state)
                if (batch.get("integrity_failure") or batch.get("git_admin", identity) != identity) and not request.get("adopt"):
                    raise ApiError("integrity_failed", "Protected state changed; inspect Git and source changes, then explicitly adopt and reverify.")
                snapshot = workspace.checkpoint(session, state, "resume")
                if orphaned or batch.get("status") == "running" or any(t["status"] in ("fixing", "repairing") for t in batch["tasks"]):
                    batch["status"] = "interrupted"
                    if not request.get("adopt") and not request.get("skip"):
                        raise ApiError("interrupted_task", "Inspect the interrupted task, then adopt and reverify or explicitly skip it.")
                if snapshot["tree"] != batch["checkpoint"]["tree"] and not request.get("adopt"):
                    raise ApiError("worktree_changed", "Inspect partial/manual changes, then explicitly adopt and reverify.")
                if request.get("adopt"):
                    batch["checkpoint"] = snapshot
                    batch["git_admin"] = identity
                    batch.pop("integrity_failure", None)
                    for task in batch["tasks"]:
                        if task["status"] not in ("skipped", "blocked"):
                            task.setdefault("before", {"tree": batch["baseline"]})
                            task.setdefault("implementation", {"summary": "Manually adopted changes"})
                            task.update(status="reviewing", verification_superseded=True)
                skip = request.get("skip", [])
                if not isinstance(skip, list) or any(t not in {x["id"] for x in batch["tasks"]} for t in skip):
                    raise ApiError("invalid_request", "Choose existing tasks to skip.")
                for task in batch["tasks"]:
                    if task["id"] in skip:
                        task["status"] = "skipped"
                if batch.get("discussion_stale"):
                    if not request.get("replan"):
                        raise ApiError("stale_discussion", "Feedback changed; inspect it and explicitly replan unfinished work.")
                    # Completed outcomes with changed source feedback must also be reconsidered.
                    current = {i["key"]: i["fingerprint"] for i in state["feedback"]}
                    for task in batch["tasks"]:
                        if any(current.get(k) != batch["fingerprints"].get(k) for k in task["items"]):
                            task["status"] = "queued"
                if batch["settings"] != state["settings"]:
                    raise ApiError("settings_changed", "Restore the batch's saved provider settings before resuming it.")
            else:
                workspace.clean(session, state)
                baseline = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
                if not workspace.ancestor(session, remote, baseline):
                    raise ApiError("diverged", "Owner branch diverged from GitHub; reconcile it in Magit.")
                items = session.items(state, request.get("items"))
                if any(not i["actionable"] for i in items):
                    raise ApiError("invalid_target", "Choose feedback with an actionable discussion, not an empty verdict.")
                snapshot = workspace.checkpoint(session, state, "batch baseline")
                batch = {"id": uuid.uuid4().hex, "created_at": now(), "baseline": baseline, "remote_head": remote,
                         "baseline_ref": workspace.pin_commit(session, baseline),
                         "items": [i["key"] for i in items], "fingerprints": {i["key"]: i["fingerprint"] for i in items},
                         "settings": copy.deepcopy(state["settings"]), "checkpoint": snapshot, "tasks": [],
                         "git_admin": workspace.admin(session, state),
                         "verify_only": bool(request.get("verify_only")),
                         "plans": [], "attempts": [], "repairs": 0, "status": "planning"}
                state["batches"].append(batch)
                session.save(state)
            workspace.trust(session, state, batch["checkpoint"]["tree"])
            models.preflight(session, state)
            if not batch["tasks"] or request.get("replan"):
                completed = [t for t in batch["tasks"] if t["status"] == "complete"]
                covered = {k for t in completed for k in t["items"]}
                keys = [k for k in batch["items"] if k not in covered]
                if keys:
                    data = context(session, state, batch, keys, batch["baseline"])
                    plan = models.invoke(session, state, batch, "plan", data)
                    tasks = validate_plan(plan, session.items(state, keys))
                    prefix = f"p{len(batch['plans']) + 1}-"
                    for task in tasks:
                        task["id"] = prefix + task["id"]
                        task["depends_on"] = [prefix + dep for dep in task["depends_on"]]
                    batch["plans"].append({"at": now(), "previous_tasks": copy.deepcopy(batch["tasks"]), "plan": plan})
                    batch["tasks"] = completed + tasks
                batch["discussion_stale"] = False
                batch["fingerprints"] = {i["key"]: i["fingerprint"] for i in session.items(state, batch["items"])}
            batch["status"] = "running"
            batch.pop("error", None)
            session.save(state)
            for task in batch["tasks"]:
                if task["status"] in ("complete", "skipped"):
                    continue
                statuses = {t["id"]: t["status"] for t in batch["tasks"]}
                if any(statuses[d] != "complete" for d in task["depends_on"]):
                    task["status"] = "blocked"
                    session.save(state)
                    continue
                check_fresh(session, state, batch)
                emit("progress", message=f"Owner task {task['id']}: {task['rationale']}")
                if not run_task(session, state, batch, task):
                    batch["status"] = "paused"
                    session.save(state)
                    return session.view(state)
            check_fresh(session, state, batch)
            final_review(session, state, batch)
            check_fresh(session, state, batch)
            session.save(state)
            return session.view(state)
        except BaseException as exc:
            if batch is not None:
                batch.update(status="interrupted" if getattr(exc, "code", None) == "cancelled" else "paused", error=str(exc))
                session.save(state)
            raise
