"""Dependency-aware owner batches with explicit, recoverable task transitions."""
import copy
import re
import uuid

from .common import ApiError, digest, now
from .events import emit
from .operations import operation
from . import owner_git as workspace, owner_providers as models, owner_commit_plan, owner_auto_commit, owner_contracts, owner_recovery, owner_plan_recovery
from .owner_state import refresh, superseded_batches, active_feedback_keys


def validate_plan(value, items):
    tasks = value.get("tasks") if isinstance(value, dict) else None
    keys = [i["key"] for i in items]
    if not isinstance(tasks, list) or not tasks:
        raise ApiError("invalid_plan", "Planner returned no tasks.")
    issue_keys = {i["key"] for i in items if i.get("kind") == "issue_plan"}
    found, covered = {}, []
    for task in tasks:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task["id"]) or task["id"] in found
                or not isinstance(task.get("items"), list) or not task["items"]
                or any(not isinstance(k, str) for k in task["items"])
                or len(set(task["items"])) != len(task["items"])
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
        if "verification_cases" in task:
            owner_contracts.cases(task["verification_cases"], code="invalid_plan")
        found[task["id"]] = copy.deepcopy(task)
        covered.extend(task["items"])
    if set(covered) != set(keys) or any(covered.count(k) != 1 for k in keys if k not in issue_keys):
        raise ApiError("invalid_plan", "Plan must cover every selected item; ordinary feedback must occur exactly once.")
    unknown = [f"{task['id']} -> {dep}" for task in tasks for dep in task["depends_on"] if dep not in found]
    if unknown:
        raise ApiError("invalid_plan", "Unknown task prerequisites: " + "; ".join(unknown)
                       + ". depends_on must use task IDs from this plan: " + ", ".join(found) + ".")
    ordered, done = [], set()
    while len(done) < len(found):
        ready = [t for t in found.values() if t["id"] not in done and set(t["depends_on"]) <= done]
        if not ready:
            raise ApiError("invalid_plan", "Task dependencies contain a cycle.")
        task = min(ready, key=lambda t: min(keys.index(k) for k in t["items"]))
        task.update(status="queued", repairs=0)
        ordered.append(task)
        done.add(task["id"])
    # Repeated issue outcomes are ordered implementation steps, not independent
    # claims that the whole issue is fixed. Require an explicit dependency chain.
    previous = {}
    for task in ordered:
        for key in set(task['items']) & issue_keys:
            if key in previous and previous[key] not in task['depends_on']:
                raise ApiError('invalid_plan', 'Successive steps for an issue must depend on its previous task ID.')
            previous[key] = task['id']
    return ordered


def validate_result(value, keys):
    # Review models classify the assessment, not whether the implementation is
    # finished. Keep the derived legacy field for saved-state/API consumers.
    # Old reports remain readable; never guess their intent from prose.
    if isinstance(value, dict) and 'assessment' in value:
        assessment = value['assessment']
        if assessment not in ('passed', 'changes_requested', 'incomplete'):
            raise ApiError('invalid_result', 'Unknown owner review assessment.')
        complete = assessment != 'incomplete'
        if 'complete' in value and (type(value['complete']) is not bool or value['complete'] != complete):
            raise ApiError('invalid_result', 'Owner review assessment contradicts its completion field.')
        value = {**value, 'complete': complete}
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
    if value.get('assessment') == 'passed' and (value['blocking'] or any(o['status'] == 'clarify' for o in outcomes)):
        raise ApiError('invalid_result', 'A passed owner review cannot contain blockers or unanswered questions.')
    if value.get('assessment') == 'changes_requested' and (not value['blocking'] or any(o['status'] == 'clarify' for o in outcomes)):
        raise ApiError('invalid_result', 'Changes requested requires concrete defects, not unanswered questions.')
    return value


def successful(value):
    return value["complete"] and not value["blocking"] and all(o["status"] != "clarify" for o in value["outcomes"])


def repairable(value):
    return value['complete'] and value['blocking'] and all(o['status'] != 'clarify' for o in value['outcomes'])


def implementation_ready(session, state, batch, task):
    report = task["implementation"]
    if ((report['complete'] is False or any(o['status'] == 'clarify' for o in report['outcomes']))
            and state.get('issue_workflow') and batch and batch.get('automatic_recovery')):
        # Incomplete implementation is a claim for independent technical review,
        # not proof that user input is required or permission to commit.
        task['implementation_assessment_tree'] = batch['checkpoint']['tree']
        session.save(state)
        return True
    task.pop('implementation_assessment_tree', None)
    if not report["complete"] or any(o["status"] == "clarify" for o in report["outcomes"]):
        task["status"] = "needs_attention"
        session.save(state)
        return False
    return True


def legacy_issue_replan(session, state, batch):
    """Upgrade only an untouched, incomplete pre-step issue attempt on resume."""
    tasks = batch.get('tasks', [])
    if (batch.get('issue_task_format', 1) >= 2 or batch.get('status') != 'paused'
            or batch.get('verify_only') or batch.get('commit_execution') or batch.get('integrity_failure')
            or len(tasks) != 1 or tasks[0].get('status') != 'needs_attention'):
        return False
    report = tasks[0].get('implementation', {})
    if (report.get('complete') is not False or tasks[0].get('review')
            or any(o['status'] == 'clarify' for o in report.get('outcomes', []))):
        return False
    issue_keys = {i['key'] for i in state['feedback'] if i['kind'] == 'issue_plan' and not i.get('missing')}
    if not batch['items'] or not set(batch['items']) <= issue_keys:
        return False
    baseline = session.repo.git('rev-parse', batch['baseline'] + '^{tree}').stdout.strip()
    return batch.get('checkpoint', {}).get('tree') == baseline


def execution_failure(batch):
    """Expose saved evidence without conflating incomplete work with questions."""
    execution = batch.get('commit_execution', {})
    if execution.get('error'):
        return {'stage': 'commit', 'summary': execution['error'], 'blocking': [execution['error']],
                'next_action': 'inspect_owner'}
    task = next((t for t in batch.get('tasks', []) if t['status'] not in ('complete', 'skipped', 'blocked')), None)
    if task:
        implementation = task.get('implementation', {})
        assessed = (task.get('implementation_assessment_tree') is not None and
                    task.get('after', {}).get('tree') == task['implementation_assessment_tree'] and
                    task.get('assessment_review_report') == digest(implementation))
        role = 'fix' if implementation and not assessed and (implementation.get('complete') is False or
                 any(o['status'] == 'clarify' for o in implementation.get('outcomes', []))) else 'review'
        report = task.get('implementation' if role == 'fix' else 'review', {})
    else:
        role, report = 'review', batch.get('review', {})
    blockers = list(report.get('blocking', []))
    questions = [o['evidence'] for o in report.get('outcomes', []) if o['status'] == 'clarify']
    blockers = list(dict.fromkeys(blockers + questions))
    summary = batch.get('error') or report.get('summary') or 'Owner execution is incomplete.'
    if task and task.get('planning_recovery_exhausted'):
        return {'stage': 'planning', 'task': task['id'],
                'summary': 'Automatic planning repair has already been used; the remaining dependency needs maintainer attention.',
                'blocking': blockers, 'next_action': 'inspect_owner'}
    if task and task.get('recovery_request'):
        return {'stage': 'verification', 'task': task['id'], 'summary': summary,
                'blocking': blockers, 'next_action': 'await_recovery_authorization',
                'recovery_request': copy.deepcopy(task['recovery_request'])}
    attempt = next((a.get('path') for a in reversed(batch.get('attempts', [])) if a.get('role') == role), None)
    retry = report.get('complete') is False and not questions and not batch.get('integrity_failure')
    return {'stage': 'implementation' if role == 'fix' else 'verification',
            'task': task['id'] if task else None, 'summary': summary, 'blocking': blockers,
            'attempt': attempt, 'next_action': 'retry' if retry else 'inspect_owner'}


def context(session, state, batch, keys, before):
    batch_diff = workspace.diff(session, batch["baseline"], batch["checkpoint"]["tree"])
    task_diff = workspace.diff(session, before, batch["checkpoint"]["tree"])
    completed = copy.deepcopy([t for t in batch['tasks'] if t['status'] == 'complete'])
    # Older saved tasks can contain an unbounded inventory of ignored files.
    for task in completed:
        for field in ('before', 'after'):
            snapshot = task.get(field, {})
            if 'ignored' in snapshot:
                snapshot.setdefault('ignored_count', len(snapshot['ignored']))
                snapshot['ignored'] = snapshot['ignored'][:20]
    approved = next((i["source"] for i in state["feedback"] if i["kind"] == "issue_plan" and not i.get("missing")), None)
    return {**({"approved_issue_plan": approved} if approved and state.get("issue_workflow") else {}),
            "feedback": session.items(state, keys), "baseline": batch["baseline"],
            "batch_diff": batch_diff, **({'task_diff': task_diff} if task_diff != batch_diff else {}),
            "completed_tasks": completed,
            "remaining_tasks": [{k: copy.deepcopy(t[k]) for k in
                ('id', 'items', 'depends_on', 'expected', 'status') if k in t}
                for t in batch['tasks'] if t['status'] not in ('complete', 'skipped')]}


def clarification_questions(batch):
    """Only current independent review can promote a claim to a user question."""
    tree = batch.get('checkpoint', {}).get('tree')
    if not tree:
        return []
    reports = []
    for task in batch.get('tasks', []):
        review = task.get('review', {})
        implementation = task.get('implementation', {})
        if (task.get('after', {}).get('tree') == tree
                and owner_contracts.matches(review, owner_contracts.task_cases(task), tree)
                and (not any(o.get('status') == 'clarify' for o in implementation.get('outcomes', []))
                     or task.get('assessment_review_report') == digest(implementation))):
            reports.append(review)
    review = batch.get('review', {})
    if (review.get('case_tree', batch.get('review_tree')) == tree
            and owner_contracts.matches(review, owner_contracts.combined(batch.get('tasks', [])), tree)):
        reports.append(review)
    return list(dict.fromkeys(o['evidence'] for report in reports for o in report.get('outcomes', [])
                             if o.get('status') == 'clarify'))


def run_task(session, state, batch, task):
    # execute() has already checked source, Git state, feedback, and settings.
    # An unfinished assessment retries review. A completed rejection instead
    # supplies the existing findings to the bounded repair path below.
    saved_review = None
    recovery = None
    task.pop('planning_recovery_exhausted', None)
    implementation = task.get('implementation', {})
    tree = batch['checkpoint']['tree']
    if (task['status'] == 'needs_attention' and state.get('issue_workflow')
            and batch.get('automatic_recovery')
            and (implementation.get('complete') is False
                 or any(o['status'] == 'clarify' for o in implementation.get('outcomes', [])))
            and tree == implementation.get('case_tree', task.get('implementation_assessment_tree'))
            and owner_contracts.matches(implementation, owner_contracts.task_cases(task), tree)):
        # Reuse the saved partial implementation. Do not discard its diagnosis
        # by launching another blind fix on every periodic invocation.
        task['implementation_assessment_tree'] = tree
        task['status'] = 'reviewing'
        if (task.get('after', {}).get('tree') == tree and task.get('review')
                and task['review'].get('complete') is True
                and task.get('assessment_review_report') == digest(implementation)
                and owner_contracts.matches(task['review'], owner_contracts.task_cases(task), tree)):
            saved_review = task['review']
    if (task["status"] == "needs_attention" and task.get('review')
            and task.get("implementation", {}).get("complete") is True
            and task.get("after", {}).get("tree") == batch["checkpoint"]["tree"]
            and owner_contracts.matches(task["review"], owner_contracts.task_cases(task), batch["checkpoint"]["tree"])):
        task["status"] = "reviewing"
        if task['review']['complete'] and task['review']['blocking']:
            saved_review = task['review']
    if batch.get("verify_only") and task["status"] != "reviewing":
        task.update(before=batch["checkpoint"], status="reviewing", implementation={"summary": "Read-only verification requested"})
    if task["status"] != "reviewing":
        task["before"] = task.pop('recovery_baseline', batch["checkpoint"])
        task["status"] = "fixing"
        session.save(state)
        data = context(session, state, batch, task["items"], task["before"]["tree"])
        data["task"] = {k: task[k] for k in ("id", "rationale", "expected")}
        data["verification_cases"] = owner_contracts.task_cases(task)
        task["implementation"] = validate_result(models.invoke(session, state, batch, "fix", data), task["items"])
        owner_contracts.implementation(task, task["implementation"], batch["checkpoint"]["tree"])
        if not implementation_ready(session, state, batch, task):
            return False
        task["status"] = "reviewing"
        session.save(state)
    while True:
        data = context(session, state, batch, task["items"], task["before"]["tree"])
        data["task"] = {k: task[k] for k in ("id", "rationale", "expected")}
        data["verification_cases"] = owner_contracts.task_cases(task)
        data["implementation_report"] = task["implementation"]
        if task.get("review", {}).get("complete") is False:
            data["previous_incomplete_review"] = task["review"]
        review = saved_review if saved_review is not None else validate_result(models.invoke(session, state, batch, "review", data), task["items"])
        saved_review = None
        owner_contracts.bind(review, owner_contracts.task_cases(task), batch["checkpoint"]["tree"])
        task["review"] = review
        task["after"] = batch["checkpoint"]
        if task.get('implementation_assessment_tree') == batch['checkpoint']['tree']:
            task['assessment_review_report'] = digest(task['implementation'])
        if recovery is not None:
            recovery.update(status='passed' if successful(review) else 'rejected' if repairable(review) else 'incomplete',
                            reviewed_tree=batch['checkpoint']['tree'])
            recovery = None
        session.save(state)
        if successful(review):
            task["status"] = "complete"
            session.save(state)
            return True
        can_repair = not batch.get('verify_only') and repairable(review)
        if can_repair and task['repairs'] >= 1:
            recovery = owner_recovery.reserve(state, batch, task, review)
            session.save(state)
        if can_repair and (task['repairs'] < 1 or recovery is not None):
            task["repairs"] += 1  # Persist before launching; crashes do not grant extra repairs.
            task["status"] = "repairing"
            session.save(state)
            data["review_findings"] = review
            data["verification_cases"] = owner_contracts.task_cases(task)
            if recovery is not None:
                data['focused_recovery'] = {
                    'instruction': 'Repair the concrete review defects within this task. Preserve existing '
                    'cases and already-correct behavior; add focused regression evidence. Do not replan '
                    'the whole issue or weaken verification.',
                    'attempt': len(state['issue_recovery']['attempts']),
                    'limit': state['issue_recovery']['limit']}
                emit('progress', message='Automatically repairing rejected task within the issue recovery allowance.')
            task["implementation"] = validate_result(models.invoke(session, state, batch, "fix", data), task["items"])
            owner_contracts.implementation(task, task["implementation"], batch["checkpoint"]["tree"])
            if not implementation_ready(session, state, batch, task):
                if recovery is not None:
                    recovery['status'] = 'implementation_incomplete'
                    session.save(state)
                return False
            task["status"] = "reviewing"
            session.save(state)
            continue
        if not batch.get('verify_only') and review['complete'] and review['blocking'] and task['repairs'] >= 1:
            emit('progress', message=('Automatic recovery allowance exhausted; see the issue for retry authorization.'
                 if task.get('recovery_request') else
                 'Automatic task repair limit reached. Inspect and adopt manual corrections, or explicitly replan unfinished work.'))
        task["status"] = "needs_attention"
        session.save(state)
        return False


def final_review(session, state, batch):
    issue_keys = {i['key'] for i in state['feedback'] if i['kind'] == 'issue_plan'}
    if any(set(t['items']) & issue_keys and t['status'] != 'complete' for t in batch['tasks']):
        batch['status'] = 'paused'
        batch['error'] = 'Every approved issue step must complete before final verification; replan unfinished work.'
        return
    keys = list(dict.fromkeys(k for t in batch["tasks"] if t["status"] == "complete" for k in t["items"]))
    if not keys:
        batch["status"] = "paused"
        return
    while True:
        batch["status"] = "reviewing"
        session.save(state)
        data = context(session, state, batch, keys, batch["baseline"])
        data["verification_cases"] = owner_contracts.combined(batch["tasks"])
        data["scope"] = "Review the combined diff and all completed outcomes, including interactions between tasks. For approved issues, verify the entire approved plan and every acceptance criterion, not just individual step outcomes."
        if batch.get("review", {}).get("complete") is False:
            data["previous_incomplete_review"] = batch["review"]
        review = validate_result(models.invoke(session, state, batch, "review", data), keys)
        owner_contracts.bind(review, data["verification_cases"], batch["checkpoint"]["tree"])
        batch["review"] = review
        batch['review_tree'] = batch['checkpoint']['tree']
        if successful(review):
            batch.update(status="ready", final_tree=batch["checkpoint"]["tree"], completed_at=now(),
                         verified_items=keys, verified_fingerprints={i["key"]: i["fingerprint"] for i in session.items(state, keys)},
                         verified_base=state["metadata"]["baseRefOid"])
            for task in batch["tasks"]:
                if task.get("after") and task["after"]["tree"] != batch["final_tree"]:
                    task["verification_superseded"] = True
            owner_commit_plan.update(session, batch, batch["baseline"])
            return
        if not batch.get("verify_only") and repairable(review) and batch["repairs"] < 1:
            batch["repairs"] += 1
            batch['combined_repair_pending'] = True
            session.save(state)
            data["review_findings"] = review
            result = validate_result(models.invoke(session, state, batch, "fix", data), keys)
            if result.get('verification_cases'):
                raise ApiError('invalid_result', 'Integration repair must preserve the completed task contracts; explicitly replan new requirements.')
            owner_contracts.bind(result, data["verification_cases"], batch["checkpoint"]["tree"])
            if not result["complete"] or any(o["status"] == "clarify" for o in result["outcomes"]):
                batch["status"] = "paused"
                return
            batch.pop('combined_repair_pending', None)
            session.save(state)
            continue
        batch["status"] = "paused"
        return


def check_fresh(session, state, batch):
    refresh(session, state)
    if state["metadata"]["headRefOid"] != batch["remote_head"]:
        raise ApiError("stale_source", "GitHub head changed during the batch; reconcile in Magit before continuing.")
    if batch.get("discussion_stale"):
        raise ApiError("stale_discussion", "Selected feedback changed during the batch; inspect and replan unfinished work.")


def plan_tasks(session, state, batch, keys, planning_repair=None):
    items = session.items(state, keys)
    rejected = batch.get("plan_rejections", [])
    correction = {k: rejected[-1][k] for k in ("plan", "error")} if rejected else None
    # Resume also explains a previous rejected plan, including pre-upgrade runs.
    for attempt in reversed(batch["attempts"]):
        if attempt.get("role") == "plan" and isinstance(attempt.get("result"), dict):
            try:
                validate_plan(attempt["result"], items)
            except ApiError as exc:
                correction = {"plan": attempt["result"], "error": str(exc)}
            break
    fingerprints = {i["key"]: i["fingerprint"] for i in items}
    for number in range(3):
        # Re-fetch context before every call; a correction cannot bypass freshness.
        refresh(session, state)
        if state["metadata"]["headRefOid"] != batch["remote_head"]:
            raise ApiError("stale_source", "GitHub head changed during planning; reconcile before continuing.")
        if any(i["fingerprint"] != fingerprints[i["key"]] for i in session.items(state, keys)):
            raise ApiError("stale_discussion", "Selected feedback changed during planning; inspect and replan.")
        data = context(session, state, batch, keys, batch["baseline"])
        if planning_repair is not None:
            data['planning_repair'] = planning_repair
        # An explicit replan must retain known failures, not just the resulting
        # diff. Omit checkpoint inventories and local attempt paths.
        data['unfinished_tasks'] = [
            {field: copy.deepcopy(task[field]) for field in
             ('id', 'rationale', 'expected', 'status', 'verification_cases', 'implementation', 'review')
             if field in task}
            for task in batch.get('tasks', [])
            if task['status'] not in ('complete', 'skipped') and set(task['items']) & set(keys)]
        if correction:
            data["plan_correction"] = correction
        plan = models.invoke(session, state, batch, "plan", data)
        try:
            tasks = validate_plan(plan, items)
            if planning_repair is not None:
                owner_plan_recovery.validate(tasks, planning_repair)
            return plan, tasks
        except ApiError as exc:
            if exc.code != "invalid_plan":
                raise
            correction = {"plan": plan, "error": str(exc)}
            batch.setdefault("plan_rejections", []).append({**copy.deepcopy(correction), "at": now()})
            session.save(state)
            if number == 2:
                raise ApiError("invalid_plan", "Planning stopped after 3 invalid plans. " + str(exc)) from exc
            emit("progress", message=f"Plan rejected: {exc} Requesting correction {number + 1}/2.")


def repair_task_plan(session, state, batch, task):
    proposal = owner_plan_recovery.proposal(state, batch, task)
    if proposal is None:
        return False
    record = owner_plan_recovery.reserve(state, batch, proposal)
    if record is None:
        task['planning_recovery_exhausted'] = True
        session.save(state)
        return False
    session.save(state)
    keys = list(dict.fromkeys(k for t in batch['tasks'] if t['status'] not in ('complete', 'skipped') for k in t['items']))
    emit('progress', message='Repairing an internal verification dependency in the unfinished task plan.')
    try:
        plan, tasks = plan_tasks(session, state, batch, keys, planning_repair=proposal)
        check_fresh(session, state, batch)
        owner_plan_recovery.install(batch, plan, tasks)
        record['status'] = 'applied'
        session.save(state)
    except BaseException:
        record['status'] = 'failed'
        session.save(state)
        raise
    return True


def execute(session, state, request):
    """Caller holds only the owner execution lock throughout inference."""
    if "auto_commit" in request and type(request["auto_commit"]) is not bool:
        raise ApiError("invalid_request", "auto_commit must be a Boolean.")
    if request.get("auto_commit"):
        owner_auto_commit.timeout()
        if request.get("verify_only"):
            raise ApiError("invalid_request", "Read-only verification cannot automatically commit.")
    with operation({"common": str(session.repo.common), "pr": str(session.repo.pr), "kind": "owner-fix"}):
        batch = None
        completing = False
        preserve_ready = False
        try:
            # Refresh before creating a worktree; failure leaves existing source untouched.
            refresh(session, state)
            remote = workspace.prepare(session, state)
            if request.get("batch"):
                batch = next((b for b in state["batches"] if b["id"] == request["batch"]), None)
                if not batch:
                    raise ApiError("invalid_request", "Choose an existing owner batch.")
                replacements = superseded_batches(state).get(batch["id"])
                if replacements:
                    batch = None  # Preserve the original stop reason and artifacts.
                    raise ApiError("superseded_batch", "This unused batch was superseded by newer verified fixes; no resume is needed.",
                                   superseded_by=replacements)
                if request.get("auto_commit") and batch.get("verify_only"):
                    raise ApiError("invalid_request", "Read-only verification cannot automatically commit.")
                if "auto_commit" in request:
                    batch["auto_commit"] = request["auto_commit"]
                    session.save(state)
                controls = any(request.get(k) for k in ("adopt", "replan", "skip"))
                if batch.get("commit_execution") and controls:
                    head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
                    if head != batch["baseline"]:
                        completing = True
                        raise ApiError("stale_source", "This batch has entered committing; reconcile and start a new batch before replanning or adopting changes.")
                if batch["status"] == "ready" and not controls and (batch.get("auto_commit") or "auto_commit" in request or batch.get("commit_execution")):
                    if batch.get("auto_commit"):
                        completing = True
                        return owner_auto_commit.execute(session, state, batch, remote)
                    return session.view(state)
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
                skip = request.get('skip', [])
                if not isinstance(skip, list) or any(t not in {x['id'] for x in batch['tasks']} for t in skip):
                    raise ApiError('invalid_request', 'Choose existing tasks to skip.')
                interrupted = {t['id'] for t in batch['tasks'] if t['status'] in ('fixing', 'repairing')}
                # Older versions did not persist a combined-repair marker.
                last = batch['attempts'][-1] if batch['attempts'] else {}
                if (last.get('role') == 'fix' and last.get('status') in ('failed', 'interrupted') and not last.get('adopted_at')
                        and batch['tasks'] and all(t['status'] in ('complete', 'skipped') for t in batch['tasks'])):
                    batch['combined_repair_pending'] = True
                if orphaned or batch.get('combined_repair_pending') or batch.get("status") == "running" or interrupted:
                    batch["status"] = "interrupted"
                    if not request.get('adopt') and (batch.get('combined_repair_pending') or not interrupted or not interrupted <= set(skip)):
                        raise ApiError('interrupted_task', 'Inspect retained changes, then adopt and reverify; task skips must cover every interrupted task. A failed combined repair requires adoption.')
                if snapshot["tree"] != batch["checkpoint"]["tree"] and not request.get("adopt"):
                    raise ApiError("worktree_changed", "Inspect partial/manual changes, then explicitly adopt and reverify.")
                if request.get("adopt"):
                    batch["checkpoint"] = snapshot
                    batch["git_admin"] = identity
                    batch.pop("integrity_failure", None)
                    batch.pop('combined_repair_pending', None)
                    for attempt in batch['attempts']:
                        if attempt.get('role') == 'fix' and attempt.get('status') in ('failed', 'interrupted'):
                            attempt.setdefault('adopted_at', now())
                    for task in batch["tasks"]:
                        if task["status"] not in ("queued", "skipped", "blocked"):
                            task.setdefault("before", {"tree": batch["baseline"]})
                            task.setdefault("implementation", {"summary": "Manually adopted changes"})
                            task.update(status="reviewing", verification_superseded=True)
                for task in batch["tasks"]:
                    if task["id"] in skip:
                        task["status"] = "skipped"
                if batch.get("discussion_stale"):
                    if not request.get("replan"):
                        raise ApiError("stale_discussion", "Feedback changed; inspect it and explicitly replan unfinished work.")
                    # Completed outcomes with changed source feedback must also be reconsidered.
                    current = {i["key"]: i["fingerprint"] for i in state["feedback"] if not i.get('missing')}
                    for task in batch["tasks"]:
                        if task['status'] != 'skipped' and any(current.get(k) != batch["fingerprints"].get(k) for k in task["items"]):
                            task["status"] = "queued"
                if batch["settings"] != state["settings"]:
                    if batch.get("attempts") or batch.get("plans") or batch.get("tasks"):
                        raise ApiError("settings_changed", "Model work has already started. Restore the batch's saved provider settings (shown in its details) before resuming it.")
                    # Preflight may reject the initial choices before any model
                    # work. Let that untouched batch use corrected settings.
                    batch["settings"] = copy.deepcopy(state["settings"])
                    session.save(state)
            else:
                items = session.items(state, request.get("items"))
                if any(not i["actionable"] for i in items):
                    raise ApiError("invalid_target", "Choose feedback with an actionable discussion, not an empty verdict.")
                baseline = workspace.new_batch_head(session, state, remote)
                snapshot = workspace.checkpoint(session, state, "batch baseline")
                batch = {"id": uuid.uuid4().hex, "created_at": now(), "baseline": baseline, "remote_head": remote,
                         "baseline_ref": workspace.pin_commit(session, baseline),
                         "items": [i["key"] for i in items], "fingerprints": {i["key"]: i["fingerprint"] for i in items},
                         "settings": copy.deepcopy(state["settings"]), "checkpoint": snapshot, "tasks": [],
                         "git_admin": workspace.admin(session, state),
                         "verify_only": bool(request.get("verify_only")),
                         "auto_commit": request.get("auto_commit", False),
                         "plans": [], "attempts": [], "repairs": 0, "status": "planning"}
                state["batches"].append(batch)
                session.save(state)
            workspace.trust(session, state, batch["checkpoint"]["tree"], approval=request.get("trusted_snapshot"))
            # Only an unchanged, already verified batch survives a failed auth
            # probe. Source/feedback/settings/trust guards above still invalidate.
            preserve_ready = batch['status'] == 'ready' and not any(request.get(k) for k in ('adopt', 'replan', 'skip'))
            models.preflight(session, state)
            preserve_ready = False
            batch.pop('preflight_error', None)
            if request.get('automatic_recovery') and state.get('issue_workflow'):
                batch['automatic_recovery'] = True
            if not batch["tasks"] or request.get("replan"):
                retained = [t for t in batch["tasks"] if t["status"] in ('complete', 'skipped')]
                unfinished = {k for t in batch['tasks'] if t['status'] not in ('complete', 'skipped') for k in t['items']}
                skipped_issues = {k for t in retained if t['status'] == 'skipped' for k in t['items']
                                  if any(i['key'] == k and i['kind'] == 'issue_plan' for i in state['feedback'])}
                # An issue cannot be completed by skipping an implementation step.
                retained = [t for t in retained if not (t['status'] == 'skipped' and set(t['items']) & skipped_issues)]
                unfinished |= skipped_issues
                covered = {k for t in retained for k in t["items"]} - unfinished
                keys = [k for k in batch["items"] if k not in covered]
                if keys:
                    plan, tasks = plan_tasks(session, state, batch, keys)
                    prefix = f"p{len(batch['plans']) + 1}-"
                    for task in tasks:
                        task["id"] = prefix + task["id"]
                        task["depends_on"] = [prefix + dep for dep in task["depends_on"]]
                    batch["plans"].append({"at": now(), "previous_tasks": copy.deepcopy(batch["tasks"]), "plan": plan})
                    retained_ids = {t['id'] for t in retained}
                    for task in retained:
                        task['depends_on'] = [dep for dep in task['depends_on'] if dep in retained_ids]
                    predecessors = {k: t['id'] for t in retained for k in t['items']}
                    for task in tasks:
                        for key in task['items']:
                            if key in predecessors and predecessors[key] not in task['depends_on']:
                                task['depends_on'].append(predecessors[key])
                            predecessors[key] = task['id']
                    batch["tasks"] = retained + tasks
                    batch["issue_task_format"] = 2
                batch["discussion_stale"] = False
                active = active_feedback_keys(batch)
                batch["fingerprints"] = {i["key"]: i["fingerprint"] for i in session.items(state, active)} if active else {}
            batch["status"] = "running"
            batch.pop("error", None)
            session.save(state)
            while True:
                replanned = False
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
                        if repair_task_plan(session, state, batch, task):
                            replanned = True
                            break
                        batch["status"] = "paused"
                        session.save(state)
                        return session.view(state)
                if not replanned:
                    break
            check_fresh(session, state, batch)
            final_review(session, state, batch)
            check_fresh(session, state, batch)
            session.save(state)
            if batch["status"] == "ready" and batch.get("auto_commit"):
                completing = True
                return owner_auto_commit.execute(session, state, batch, remote)
            return session.view(state)
        except BaseException as exc:
            if batch is not None and not completing:
                if preserve_ready:
                    batch['preflight_error'] = str(exc)
                else:
                    batch.update(status="interrupted" if getattr(exc, "code", None) == "cancelled" else "paused", error=str(exc))
                session.save(state)
            raise
