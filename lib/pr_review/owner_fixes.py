"""Dependency-aware owner batches with explicit, recoverable task transitions."""
import copy
import re
import uuid

from . import owner_scope
from .forge_context import owner_change

from . import usage, owner_reports, session_trace
from .common import ApiError, digest, now
from .events import emit
from .operations import operation
from . import owner_format, acceptance, owner_git as workspace, owner_providers as models, owner_commit_plan, owner_auto_commit, owner_contracts, review_history
from .owner_state import refresh, superseded_batches, active_feedback_keys, issue_settings, can_retry_final_recovery


def validate_plan(value, items, *, recovery=False):
    if recovery:
        decision = value.get('decision') if isinstance(value, dict) else None
        if (decision not in ('repair', 'clarify', 'repeated_failure')
                or not isinstance(value.get('basis'), str) or not value['basis'].strip()):
            raise ApiError('invalid_plan', 'Recovery needs a decision and its supporting evidence.')
        if decision != 'repair' and value.get('tasks') == []:
            return []

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
        reported = [o["item"] for o in outcomes]
        raise ApiError("invalid_result", f"Expected one outcome for each of {keys}; received {reported}.",
                       outcome_coverage=True)
    if value.get('assessment') == 'passed' and (value['blocking'] or any(o['status'] == 'clarify' for o in outcomes)):
        raise ApiError('invalid_result', 'A passed owner review cannot contain blockers or unanswered questions.')
    if value.get('assessment') == 'changes_requested' and (not value['blocking'] or any(o['status'] == 'clarify' for o in outcomes)):
        raise ApiError('invalid_result', 'Changes requested requires concrete defects, not unanswered questions.')
    return value


def successful(value):
    return value["complete"] and not value["blocking"] and all(o["status"] != "clarify" for o in value["outcomes"])


def repairable(value):
    return value['complete'] and value['blocking'] and all(o['status'] != 'clarify' for o in value['outcomes'])


MAX_TASK_REPAIRS = 2


def repair_evidence(review, tree):
    """Comparable independent review evidence at the candidate before a repair."""
    return {
        'tree': tree,
        'blockers': len(set(review['blocking'])),
        'addressed': sorted(o['item'] for o in review['outcomes']
                            if o['status'] in ('fixed', 'already_addressed')),
        'unverified_cases': sorted(r['id'] for r in review.get('case_results', [])
                                   if r['status'] in ('failed', 'unavailable')),
    }


def legacy_repair_evidence(batch, task, review, tree):
    """Recover the baseline only from a matching review/fix/review sequence."""
    attempts = batch.get('attempts', [])[-3:]
    if ([a.get('role') for a in attempts] != ['review', 'fix', 'review']
            or any(a.get('status') != 'complete' for a in attempts)):
        return None
    first, fix, last = attempts
    before = first.get('after', {}).get('tree')
    prior = first.get('result') or {}
    latest = last.get('result') or {}
    if (not before or before != first.get('before', {}).get('tree') or before != fix.get('before', {}).get('tree')
            or tree != fix.get('after', {}).get('tree') or tree != last.get('before', {}).get('tree')
            or tree != last.get('after', {}).get('tree')
            or not prior.get('blocking') or {o['item'] for o in prior.get('outcomes', [])} != set(task['items'])
            or any(latest.get(k) != review.get(k) for k in ('blocking', 'outcomes', 'case_results'))):
        return None
    return repair_evidence(prior, before)


def repair_stop_reason(batch, task, review, tree):
    if task['repairs'] == 0:
        return None
    if task['repairs'] >= MAX_TASK_REPAIRS:
        return f'Automatic task repair limit reached ({MAX_TASK_REPAIRS} repairs).'
    prior = task.get('repair_evidence') or legacy_repair_evidence(batch, task, review, tree)
    passing_cases = {r['id'] for r in review.get('case_results', [])
                     if r['status'] in ('tested', 'source_traced')}
    current = repair_evidence(review, tree)
    if prior and tree != prior['tree'] and (
            current['blockers'] < prior['blockers']
            or set(current['addressed']) > set(prior['addressed'])
            or passing_cases.intersection(prior['unverified_cases'])):
        return None
    return 'The last repair showed no verified progress on the remaining defects.'


def recover_completed_repair(batch, snapshot, last):
    """Resume independent review of a completed repair whose case contract can now be validated."""
    active = [t for t in batch['tasks'] if t['status'] in ('repairing', 'fixing')]
    if (batch.get('error') not in ('Case evidence must cover each required case exactly once.',
                                   'Supply at most eight focused verification cases per task.',
                                   'Inspect retained changes, then adopt and reverify; task skips must cover every interrupted task. A failed combined repair requires adoption.')
            or len(active) != 1 or active[0]['status'] != 'repairing' or last.get('role') != 'fix'
            or last.get('status') != 'complete' or last.get('after') != batch['checkpoint']
            or last.get('git_admin_before') != batch.get('git_admin')
            or snapshot['tree'] != batch['checkpoint']['tree'] or batch.get('combined_repair_pending')):
        return
    task = active[0]
    if last.get('before', {}).get('tree') != task.get('after', {}).get('tree'):
        return
    # A unique checkpoint ref also distinguishes a fresh attempt that happened
    # to produce the same tree. Match both ends to this task's reviewed repair.
    if not last['after'].get('ref') or not isinstance(last.get('result'), dict):
        return
    candidate = copy.deepcopy(task)
    try:
        report = validate_result(copy.deepcopy(last['result']), task['items'])
        if not successful(report):
            return
        history = task.get('recovery_attempts', [])
        if history and len(batch['attempts']) >= 2:
            recovery = history[-1]
            planner = batch['attempts'][-2]
            if (planner.get('role') == 'plan' and planner.get('status') == 'complete'
                    and recovery['tree'] == last['before']['tree'] and not recovery['pending']
                    and planner.get('result') == recovery['plan']):
                candidate['verification_cases'] = owner_contracts.recovery_cases(task, recovery['plan'])
        planned_additions = owner_contracts.task_cases(candidate) != owner_contracts.task_cases(task)
        owner_contracts.implementation(candidate, report, snapshot['tree'])
    except ApiError:
        return
    if not planned_additions and not (len(owner_contracts.task_cases(task)) <= owner_contracts.MAX_CASES
            < len(owner_contracts.task_cases(candidate))):
        return
    task.update(verification_cases=candidate['verification_cases'], case_contract_version=1,
                implementation=report, status='reviewing')
    emit('progress', message=f"Owner task {task['id']}: recovered completed repair; verifying all saved cases.")


def implementation_ready(session, state, batch, task):
    report = task["implementation"]
    while automatic(batch) and (not report['complete'] or any(o['status'] == 'clarify' for o in report['outcomes'])):
        data = context(session, state, batch, task['items'], task['before']['tree'])
        data.update(task={k: task[k] for k in ('id', 'rationale', 'expected')},
                    verification_cases=owner_contracts.task_cases(task), implementation_report=report)
        if not recovery_plan(session, state, batch, task, data, report):
            break
        task['status'] = 'repairing'
        session.save(state)
        report = validate_result(dispatch_repair(session, state, batch, task, data), task['items'])
        owner_contracts.implementation(task, report, batch['checkpoint']['tree'])
        task['implementation'] = report
    if not report["complete"] or any(o["status"] == "clarify" for o in report["outcomes"]):
        task["status"] = "needs_attention"
        session.save(state)
        return False
    return True


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
            **({'repair_scope': owner_scope.contract(batch)} if batch.get('execution_kind') != 'issue_harness' else {}),
            "feedback": session.items(state, keys), "baseline": batch["baseline"],
            "batch_diff": batch_diff, **({'task_diff': task_diff} if task_diff != batch_diff else {}),
            "completed_tasks": completed, **owner_format.review_context(batch)}


def automatic(batch):
    return batch.get('auto_commit') and not batch.get('verify_only')


def diagnosis(report):
    questions = [o['evidence'] for o in report['outcomes'] if o['status'] == 'clarify']
    return ' · '.join(dict.fromkeys([*report['blocking'], *questions, report['summary']]))


def reset_recovery(target):
    target.pop('recovery_stop', None)
    target.pop('verification_blocked', None)
    target.pop('verification_retry_tree', None)
    attempts = target.pop('recovery_attempts', None)
    if attempts:
        target.setdefault('previous_recoveries', []).append(attempts)


def recovery_plan(session, state, batch, target, data, report):
    """Persist alternate approaches against the original contract across resume."""
    cases = sorted((r['id'], r['status']) for r in report.get('case_results', []))
    questions = [o for o in report['outcomes'] if o['status'] == 'clarify']
    failures = sorted(set(report['blocking']))
    signature = digest([sorted((o['item'], o['status']) for o in report['outcomes']), cases, failures,
                        sorted(o['evidence'] for o in questions)])
    history = target.setdefault('recovery_attempts', [])
    prior = next((entry for entry in history if entry['signature'] == signature), None)
    if prior and prior.get('pending') and prior['tree'] == batch['checkpoint']['tree']:
        data['recovery_plan'] = prior['plan']
        return True
    if prior:
        approach = ' / '.join(t['rationale'] for t in prior['plan']['tasks'])
        reason = 'Human decision required' if questions else 'Repair loop after alternative plan'
        target['recovery_stop'] = f'{reason}: {diagnosis(report)}. Attempted: {approach}'
        return False
    check_fresh(session, state, batch)
    emit('progress', message='Automatically replanning: ' + diagnosis(report))
    earlier = [entry for group in target.get('previous_recoveries', []) for entry in group]
    recovery = dict(findings={k: v for k, v in report.items() if k != 'observations'}, verification_cases=data['verification_cases'],
                    previous_approaches=history, earlier_replanned_approaches=earlier,
                    instruction='Choose a different approach within the original contract. For repair, explain '
                                'the approach and cite the source that answers any clarification. Consider simplifying '
                                'a previous repair that introduced a new obligation. Choose clarify '
                                'if a human decision is needed. Choose repeated_failure only if earlier attempts '
                                'show the same failing behavior without a lasting gain. Cite those attempts.')
    batch['recovery_planning'] = target.get('id')
    session.save(state)
    plan, _ = plan_tasks(session, state, batch, [i['key'] for i in data['feedback']], recovery)
    decision = plan['decision']
    basis = plan['basis']
    if decision == 'clarify':
        target['recovery_stop'] = 'Human decision required: ' + diagnosis(report) + ' · ' + basis
        return False
    if decision == 'repeated_failure':
        target['recovery_stop'] = 'Repair loop: ' + diagnosis(report) + ' · ' + basis
        return False
    history.append(dict(signature=signature, plan=plan, evidence=report, tree=batch['checkpoint']['tree'], pending=True))
    data['recovery_plan'] = plan
    session.save(state)
    return True


def dispatch_repair(session, state, batch, target, data):
    if data.get('recovery_plan'):
        if target is not batch:
            required = owner_contracts.recovery_cases(target, data['recovery_plan'])
            target['verification_cases'] = required
            data['verification_cases'] = required
        batch.pop('recovery_planning', None)
        target['recovery_attempts'][-1]['pending'] = False
        session.save(state)
    return owner_reports.invoke(session, state, batch, 'fix', data)


def retry_verification(session, state, batch, target, report):
    tree = batch['checkpoint']['tree']
    if target.get('verification_retry_tree') == tree:
        target['verification_blocked'] = True
        target['recovery_stop'] = 'Verification still unavailable after retry: ' + diagnosis(report)
        return False
    check_fresh(session, state, batch)
    target['verification_retry_tree'] = tree
    if target is batch:
        batch.setdefault('review_reruns', {})['synthesis'] = uuid.uuid4().hex
    session.save(state)
    emit('progress', message='Retrying verification on the saved candidate: ' + diagnosis(report))
    return True


def task_review(session, state, batch, task, data):
    for attempt in range(2):
        try:
            report = owner_reports.invoke(session, state, batch, 'review', data)
            return validate_result(report, task['items'])
        except ApiError as exc:
            correctable = exc.details.get('repair_scope') or (automatic(batch) and exc.details.get('outcome_coverage'))
            if attempt or not correctable:
                raise
            check_fresh(session, state, batch)
            data['report_correction'] = str(exc)
            emit('progress', message=f"Retrying task {task['id']} verification: {exc}")


def run_task(session, state, batch, task):
    # execute() has already checked source, Git state, feedback, and settings.
    # An unfinished assessment retries review. A completed rejection instead
    # supplies the existing findings to the bounded repair path below.
    saved_review = None
    task.pop('repair_stop_reason', None)
    resuming_plan = automatic(batch) and batch.get('recovery_planning') == task['id']
    if ((task["status"] == 'needs_attention' or (resuming_plan and task['status'] == 'reviewing'))
            and task.get('review')
            and task.get("implementation", {}).get("complete") is True
            and task.get("after", {}).get("tree") == batch["checkpoint"]["tree"]
            and owner_contracts.matches(task["review"], owner_contracts.task_cases(task), batch["checkpoint"]["tree"])):
        task["status"] = "reviewing"
        prior = task['review']
        if prior['complete'] and prior['blocking'] and 'repair_basis' in prior:
            owner_scope.validate(prior['repair_basis'], prior['blocking'], owner_scope.contract(batch), task['items'])
            saved_review = prior
    if batch.get("verify_only") and task["status"] != "reviewing":
        task.update(before=batch["checkpoint"], status="reviewing", implementation={"summary": "Read-only verification requested"})
    if resuming_plan and task['status'] in ('fixing', 'repairing'):
        pending = next((e for e in task.get('recovery_attempts', []) if e.get('pending')), None)
        if pending and pending['evidence'].get('assessment') == 'changes_requested' and 'repair_basis' not in pending['evidence']:
            # Reclassify a pre-upgrade review before dispatching its old plan.
            reset_recovery(task)
            batch.pop('recovery_planning', None)
        elif pending:
            data = context(session, state, batch, task['items'], task['before']['tree'])
            data.update(task={k: task[k] for k in ('id', 'rationale', 'expected')},
                        verification_cases=owner_contracts.task_cases(task), recovery_plan=pending['plan'],
                        review_findings=pending['evidence'], implementation_report=task['implementation'])
            task['implementation'] = validate_result(dispatch_repair(session, state, batch, task, data), task['items'])
            owner_contracts.implementation(task, task['implementation'], batch['checkpoint']['tree'])
        if not implementation_ready(session, state, batch, task):
            return False
        task['status'] = 'reviewing'
        session.save(state)
    if task["status"] != "reviewing":
        task["before"] = batch["checkpoint"]
        task["status"] = "fixing"
        session.save(state)
        data = context(session, state, batch, task["items"], task["before"]["tree"])
        data["task"] = {k: task[k] for k in ("id", "rationale", "expected")}
        data["verification_cases"] = owner_contracts.task_cases(task)
        task["implementation"] = validate_result(dispatch_repair(session, state, batch, task, data), task["items"])
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
        review = saved_review if saved_review is not None else task_review(session, state, batch, task, data)
        saved_review = None
        owner_contracts.bind(review, owner_contracts.task_cases(task), batch["checkpoint"]["tree"])
        owner_scope.remember_observations(batch, review)
        task["review"] = review
        task["after"] = batch["checkpoint"]
        session.save(state)
        if successful(review):
            task["status"] = "complete"
            session.save(state)
            return True
        can_repair = not batch.get('verify_only') and repairable(review)
        tree = batch['checkpoint']['tree']
        stop_reason = repair_stop_reason(batch, task, review, tree) if can_repair else None
        alternate = False
        if review.get('repair_basis') is not None and any(o['status'] == 'clarify' for o in review['outcomes']):
            task.update(status='needs_attention', recovery_stop=diagnosis(review))
            session.save(state)
            return False
        if automatic(batch):
            clarify = any(o['status'] == 'clarify' for o in review['outcomes'])
            if clarify or stop_reason:
                alternate = recovery_plan(session, state, batch, task, data, review)
            elif not review['complete'] and retry_verification(session, state, batch, task, review):
                continue
        if alternate or (can_repair and not stop_reason):
            task['repair_evidence'] = repair_evidence(review, tree)
            emit('progress', message=f"Owner task {task['id']}: automatic repair {task['repairs'] + 1}.")
            task['trace_result'] = session_trace.decision('repair_allocated', sources=[task.get('trace_result')], key=f"{batch['id']}:{task['id']}:{task['repairs']}")
            task["repairs"] += 1  # Persist before launching; crashes do not grant extra repairs.
            task["status"] = "repairing"
            session.save(state)
            data["review_findings"] = {k: v for k, v in review.items() if k != "observations"}
            data["verification_cases"] = owner_contracts.task_cases(task)
            task["implementation"] = validate_result(dispatch_repair(session, state, batch, task, data), task["items"])
            owner_contracts.implementation(task, task["implementation"], batch["checkpoint"]["tree"])
            if not implementation_ready(session, state, batch, task):
                return False
            task["status"] = "reviewing"
            session.save(state)
            continue
        if stop_reason and not automatic(batch):
            task['repair_stop_reason'] = stop_reason
            emit('progress', message=stop_reason + ' Inspect and adopt corrections, or replan unfinished work.')
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
        batch["checkpoint"] = owner_format.gate(session, state, batch, batch["baseline"], batch["checkpoint"], final=True)
        data = context(session, state, batch, keys, batch["baseline"])
        data["verification_cases"] = owner_contracts.combined(batch["tasks"])
        data["scope"] = "Review the combined diff and all completed outcomes, including interactions between tasks. For approved issues, verify the entire approved plan and every acceptance criterion, not just individual step outcomes."
        if batch.get("review", {}).get("complete") is False:
            data["previous_incomplete_review"] = batch["review"]
        # Final verification runs the shared review engine; per-task checks above
        # stay a single lightweight call. A repair changes the tree, which is a new candidate.
        if batch.get('execution_trace_version') == 2:
            batch['trace_candidate'] = session_trace.decision('candidate_review',
                sources=[batch.get('trace_candidate'), *[t.get('trace_result') for t in batch['tasks'] if t['status'] == 'complete']],
                key=batch['checkpoint']['tree'])
        _, review = acceptance.review_owner(session, state, batch, keys, data)
        review = validate_result(review, keys)
        owner_contracts.bind(review, data["verification_cases"], batch["checkpoint"]["tree"])
        owner_scope.remember_observations(batch, review)
        batch["review"] = review
        batch['review_tree'] = batch['checkpoint']['tree']
        if successful(review):
            batch.update(status="ready", final_tree=batch["checkpoint"]["tree"], completed_at=now(),
                         verified_items=keys, verified_fingerprints={i["key"]: i["fingerprint"] for i in session.items(state, keys)},
                         verified_base=owner_change(state)['target']['head'])
            for task in batch["tasks"]:
                if task.get("after") and task["after"]["tree"] != batch["final_tree"]:
                    task["verification_superseded"] = True
            owner_commit_plan.update(session, batch, batch["baseline"])
            return
        if 'repair_basis' in review and any(o['status'] == 'clarify' for o in review['outcomes']):
            batch.update(status='paused', recovery_stop=diagnosis(review), error=diagnosis(review))
            return
        repair = not batch.get("verify_only") and repairable(review) and batch["repairs"] < 1
        if automatic(batch) and not repair:
            if repairable(review) or any(o['status'] == 'clarify' for o in review['outcomes']):
                repair = recovery_plan(session, state, batch, batch, data, review)
            elif not review['complete'] and retry_verification(session, state, batch, batch, review):
                continue
            if batch.get('recovery_stop'):
                batch.pop('recovery_planning', None)
                batch['error'] = batch['recovery_stop']
        if repair:
            batch['trace_candidate'] = session_trace.decision('repair_allocated', sources=[session_trace.marked('acceptance_result')], key=f"{batch['id']}:combined:{batch['repairs']}")
            batch["repairs"] += 1
            batch['combined_repair_pending'] = True
            session.save(state)
            data["review_findings"] = {k: v for k, v in review.items() if k != "observations"}
            result = validate_result(dispatch_repair(session, state, batch, batch, data), keys)
            if result.get('verification_cases'):
                raise ApiError('invalid_result', 'Integration repair must preserve the completed task contracts; explicitly replan new requirements.')
            owner_contracts.bind(result, data["verification_cases"], batch["checkpoint"]["tree"])
            while automatic(batch) and (not result['complete'] or any(o['status'] == 'clarify' for o in result['outcomes'])):
                if not recovery_plan(session, state, batch, batch, data, result):
                    batch['error'] = batch['recovery_stop']
                    batch.pop('combined_repair_pending', None)
                    break
                data['implementation_report'] = result
                result = validate_result(dispatch_repair(session, state, batch, batch, data), keys)
                if result.get('verification_cases'):
                    raise ApiError('invalid_result', 'Integration repair must preserve completed task contracts.')
                owner_contracts.bind(result, data['verification_cases'], batch['checkpoint']['tree'])
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
    from .owner_state import require_fixing
    require_fixing(session, state)
    if owner_change(state)['source']['head'] != batch["remote_head"]:
        raise ApiError("stale_source", "GitHub head changed during the batch; reconcile in Magit before continuing.")
    if batch.get("discussion_stale"):
        message = ("Selected feedback changed during execution; resume the issue to reassess it."
                   if batch.get('execution_kind') == 'issue_harness' else
                   "Selected feedback changed during the batch; inspect and replan unfinished work.")
        raise ApiError("stale_discussion", message)


def plan_tasks(session, state, batch, keys, recovery=None):
    items = session.items(state, keys)
    correction = None
    if recovery is None:
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
        if owner_change(state)['source']['head'] != batch["remote_head"]:
            raise ApiError("stale_source", "GitHub head changed during planning; reconcile before continuing.")
        if any(i["fingerprint"] != fingerprints[i["key"]] for i in session.items(state, keys)):
            raise ApiError("stale_discussion", "Selected feedback changed during planning; inspect and replan.")
        data = context(session, state, batch, keys, batch["baseline"])
        # An explicit replan must retain known failures, not just the resulting
        # diff. Omit checkpoint inventories and local attempt paths.
        data['unfinished_tasks'] = [
            {field: copy.deepcopy(task[field]) for field in
             ('id', 'rationale', 'expected', 'status', 'verification_cases', 'implementation', 'review')
             if field in task}
            for task in batch.get('tasks', [])
            if task['status'] not in ('complete', 'skipped') and set(task['items']) & set(keys)]
        if recovery:
            data['automatic_recovery'] = recovery
        if correction:
            data["plan_correction"] = correction
        try:
            plan = owner_reports.invoke(session, state, batch, "plan", data)
            tasks = validate_plan(plan, items, recovery=recovery is not None)
            if recovery and plan['decision'] == 'repeated_failure' and not recovery['previous_approaches']:
                raise ApiError('invalid_plan', 'Try an alternative plan before declaring a repeated failure.')
            return plan, tasks
        except ApiError as exc:
            if exc.code != "invalid_plan":
                raise
            plan = getattr(exc, "rejected_plan", locals().get("plan", {}))
            correction = {"plan": plan, "error": str(exc)}
            if batch.get('execution_trace_version') == 2:
                session_trace.decision('format_correction', sources=[session_trace.marked('last_failed')], summary=str(exc), key=f"{batch['id']}:{len(batch.get('plan_rejections', []))}")
            batch.setdefault("plan_rejections", []).append({**copy.deepcopy(correction), "at": now()})
            session.save(state)
            if number == 2:
                raise ApiError("invalid_plan", "Planning stopped after 3 invalid plans. " + str(exc)) from exc
            emit("progress", message=f"Plan rejected: {exc} Requesting correction {number + 1}/2.")


def hold_task(session, state, batch, task, remaining):
    """Keep the rejected candidate pinned; run independent work from accepted edits."""
    if task.get('held_checkpoint'):
        return
    batch.pop('recovery_planning', None)
    unavailable = {t['id'] for t in batch['tasks'] if t.get('recovery_stop') or t['status'] == 'skipped'}
    for later in remaining:
        if unavailable.intersection(later['depends_on']):
            unavailable.add(later['id'])
    rejected = batch['checkpoint']
    if any(t['id'] not in unavailable and t['status'] != 'complete' for t in remaining):
        workspace.restore_checkpoint(session, state, batch, task['before'])
    task['held_checkpoint'] = rejected
    task['after'] = rejected
    session.save(state)


def execute(session, state, request):
    from . import owner_reports
    return owner_reports.execute(_execute, session, state, request)


def _execute(session, state, request):
    """Caller holds only the owner execution lock throughout inference."""
    retry_final = request.get('retry_final_recovery', False)
    if type(retry_final) is not bool:
        raise ApiError('invalid_request', 'retry_final_recovery must be a Boolean.')
    if retry_final and (not request.get('batch') or any(request.get(k) for k in
            ('adopt', 'replan', 'skip', 'items', 'verify_only', 'issue_harness', 'rerun_review'))):
        raise ApiError('invalid_request', 'Retry final recovery on an existing batch as a separate operation.')
    review_history.controls(request)
    if "auto_commit" in request and type(request["auto_commit"]) is not bool:
        raise ApiError("invalid_request", "auto_commit must be a Boolean.")
    if request.get("auto_commit"):
        owner_auto_commit.timeout()
        if request.get("verify_only"):
            raise ApiError("invalid_request", "Read-only verification cannot automatically commit.")
    with operation({"common": str(session.repo.common), "pr": str(session.repo.pr), "forge_project": session.repo.project.saved(), "session": str(session.path), "kind": "owner-fix"}), \
            usage.operation(usage.owner_root(session, state), legacy=bool(state.get("batches"))):
        from .checkouts import announce
        announce(state.get("worktree"), session.path, "owner")
        batch = None
        completing = False
        preserve_ready = False
        is_harness = bool(request.get("issue_harness"))
        try:
            # Refresh before creating a worktree; failure leaves existing source untouched.
            refresh(session, state)
            from .owner_state import require_fixing
            require_fixing(session, state)
            remote = workspace.prepare(session, state)
            if request.get("batch"):
                batch = next((b for b in state["batches"] if b["id"] == request["batch"]), None)
                if not batch:
                    raise ApiError("invalid_request", "Choose an existing owner batch.")
                is_harness = is_harness or batch.get("execution_kind") == "issue_harness"
                from . import issue_harness
                try:
                    if is_harness:
                        issue_harness.require_supported(batch)
                    else:
                        issue_harness.require_engine(batch)
                except ApiError:
                    batch = None  # Reject without rewriting the retired record.
                    raise
                if is_harness and ((not review_history.enabled(batch) and
                                    ('max_repair_cycles' in request or request.get('retry_review_adjudication')))
                                   or request.get('retry_review_adjudication') and batch['status'] == 'ready'):
                    raise ApiError('invalid_request', 'Review controls require a versioned batch with a retryable saved conflict.')
                from . import owner_reports
                replacements = superseded_batches(state).get(batch["id"])
                if replacements:
                    batch = None  # Preserve the original stop reason and artifacts.
                    raise ApiError("superseded_batch", "This unused batch was superseded by newer verified fixes; no resume is needed.",
                                   superseded_by=replacements)
                if retry_final:
                    if batch.get('discussion_stale'):
                        batch = None  # Refresh saved the change. Keep the original recovery diagnosis.
                        raise ApiError('stale_discussion', 'Feedback changed; inspect it and explicitly replan unfinished work.')
                    if not can_retry_final_recovery(batch):
                        batch = None  # A rejected retry must preserve the saved batch.
                        raise ApiError('invalid_request', 'Choose a batch with stopped final recovery.')
                if request.get("auto_commit") and batch.get("verify_only"):
                    raise ApiError("invalid_request", "Read-only verification cannot automatically commit.")
                if "auto_commit" in request:
                    batch["auto_commit"] = request["auto_commit"]
                    session.save(state)
                controls = retry_final or any(request.get(k) for k in ("adopt", "replan", "skip", "rerun_review"))
                if batch.get("commit_execution") and controls:
                    head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
                    if head != batch["baseline"]:
                        completing = True
                        raise ApiError("stale_source", "This batch has entered committing; reconcile and start a new batch before replanning or adopting changes.")
                if batch["status"] == "ready" and not controls and (batch.get("auto_commit") or "auto_commit" in request or batch.get("commit_execution")):
                    if batch.get("auto_commit"):
                        owner_reports.attach(session, state, batch)
                        completing = True
                        return owner_auto_commit.execute(session, state, batch, remote)
                    return session.view(state)
                orphaned = [a for a in batch["attempts"] if a["status"] == "running"]
                for attempt in orphaned:
                    attempt.update(status="interrupted", finished_at=now(), error="Owning operation ended before recording completion.")
                local_head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
                published_baseline = (not is_harness and remote == local_head == batch["baseline"]
                                      and batch["remote_head"] != remote
                                      and batch.get('environment_applied_at_remote') == batch["remote_head"]
                                      and workspace.ancestor(session, batch["remote_head"], remote))
                if local_head != batch["baseline"] or (batch["remote_head"] != remote and not published_baseline):
                    # A human commit ends execution of this batch, but its accepted
                    # tree remains eligible for subsequent commit association.
                    if batch["status"] == "ready":
                        batch = None
                    raise ApiError("stale_source", "HEAD changed; reconcile in Magit and start a new batch.")
                owner_reports.attach(session, state, batch)
                workspace.unstaged_index(session, state)
                identity = workspace.admin(session, state, None if request.get("adopt") else batch.get("git_admin"))
                if (batch.get("integrity_failure") or batch.get("git_admin", identity) != identity) and not request.get("adopt"):
                    raise ApiError("integrity_failed", "Protected state changed; inspect Git and source changes, then explicitly adopt and reverify.")
                snapshot = workspace.checkpoint(session, state, "resume")
                if is_harness:
                    from . import issue_harness
                    issue_harness.resume(batch, request, snapshot, identity)
                else:
                    skip = request.get('skip', [])
                    if not isinstance(skip, list) or any(t not in {x['id'] for x in batch['tasks']} for t in skip):
                        raise ApiError('invalid_request', 'Choose existing tasks to skip.')
                    # Older versions did not persist a combined-repair marker.
                    last = batch['attempts'][-1] if batch['attempts'] else {}
                    if not orphaned and not controls and not batch.get('integrity_failure'):
                        recover_completed_repair(batch, snapshot, last)
                    interrupted = {t['id'] for t in batch['tasks'] if t['status'] in ('fixing', 'repairing')}
                    if (last.get('role') == 'fix' and last.get('status') in ('failed', 'interrupted') and not last.get('adopted_at')
                            and batch['tasks'] and all(t['status'] in ('complete', 'skipped') for t in batch['tasks'])):
                        batch['combined_repair_pending'] = True
                    # An orphaned engine stage never forces adoption: the tree and Git
                    # admin checks above and below still require it after source changes.
                    safe_plan = (automatic(batch) and batch.get('recovery_planning')
                                 and all(a['role'] == 'plan' for a in orphaned)
                                 and snapshot['tree'] == batch['checkpoint']['tree'])
                    safe_review = (automatic(batch) and not interrupted and not batch.get('combined_repair_pending')
                                   and all(a['role'] in ('review', 'final_review') for a in orphaned)
                                   and snapshot['tree'] == batch['checkpoint']['tree']
                                   and any(t['status'] == 'reviewing' or
                                           any(e.get('pending') for e in t.get('recovery_attempts', []))
                                           for t in batch['tasks']))
                    if not (safe_plan or safe_review) and ([a for a in orphaned if a.get('kind') != 'review_engine'] or batch.get('combined_repair_pending')
                            or batch.get("status") == "running" or interrupted):
                        batch["status"] = "interrupted"
                        if not request.get('adopt') and (batch.get('combined_repair_pending') or not interrupted or not interrupted <= set(skip)):
                            raise ApiError('interrupted_task', 'Inspect retained changes, then adopt and reverify; task skips must cover every interrupted task. A failed combined repair requires adoption.')
                    if snapshot["tree"] != batch["checkpoint"]["tree"] and not request.get("adopt"):
                        raise ApiError("worktree_changed", "Inspect partial/manual changes, then explicitly adopt and reverify.")
                    if request.get("adopt"):
                        if not batch.get("verify_only"):
                            previous = batch["baseline"] if batch.get("formatter_error") else batch["checkpoint"]["tree"]
                            snapshot = owner_format.gate(session, state, batch, previous, snapshot)
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
                if published_baseline:
                    # The remote now contains the exact local baseline already used for these tasks.
                    batch["remote_head"] = remote
                same_settings = (issue_settings(batch["settings"]) == issue_settings(state["settings"]) if is_harness
                                 else batch["settings"] == state["settings"])
                if not same_settings:
                    if is_harness:
                        from .issue_settings import change_batch
                        change_batch(session, state, batch)
                    elif batch.get("attempts") or batch.get("plans") or batch.get("tasks"):
                        raise ApiError("settings_changed", "Model work has already started. Restore the batch's saved provider settings (shown in its details) before resuming it.")
                    else:
                        # Untouched interactive batches can correct preflight choices.
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
                         "git_admin": workspace.admin(session, state), "review_engine": 1,
                         "verify_only": bool(request.get("verify_only")),
                         "auto_commit": request.get("auto_commit", False),
                         "plans": [], "attempts": [], "repairs": 0, "status": "planning"}
                if not is_harness:
                    change = owner_change(state)
                    batch['repair_scope'] = dict(version=1, baseline=baseline, head=remote,
                        base=change['target']['head'], pr=state.get('metadata', {}).get('body', ''),
                        feedback=dict(batch['fingerprints']),
                        plan=next((i.get('source') for i in items if i['kind'] == 'issue_plan'), None))
                if is_harness:
                    batch.update(execution_kind='issue_harness', harness_phase='implement',
                                 harness_before=copy.deepcopy(snapshot))
                    review_history.initialize(batch, request)
                from .environment import capture
                capture(batch, session.repo.project)
                state["batches"].append(batch)
                session.save(state)
                if not is_harness:
                    from . import owner_reports
                    owner_reports.attach(session, state, batch, new=True)
            workspace.trust(session, state, batch["checkpoint"]["tree"], approval=request.get("trusted_snapshot"))
            if is_harness and request.get('batch') and not request.get('rerun_review') and any(
                    r.get('environment_setup_failed') and r['tree'] == batch['checkpoint']['tree']
                    and r.get('epoch') == batch.get('review_reruns', {}).get('synthesis')
                    for r in batch.get('verification_runs', [])):
                from .issue_settings import request_rerun
                request_rerun(session, state, batch, ['synthesis'], uuid.uuid4().hex)
            if is_harness and request.get('rerun_review'):
                from .issue_settings import request_rerun
                request_rerun(session, state, batch, request['rerun_review'], request.get('review_rerun_id'))
            # Only an unchanged, already verified batch survives a failed auth
            # probe. Source/feedback/settings/trust guards above still invalidate.
            preserve_ready = batch['status'] == 'ready' and not any(request.get(k) for k in ('adopt', 'replan', 'skip'))
            if is_harness:
                review_history.resume_guard(session, state, batch, request)
            if any(request.get(k) for k in ('adopt', 'replan', 'skip')):
                batch.pop('recovery_planning', None)
                targets = batch['tasks'] if request.get('adopt') or request.get('replan') else [
                    t for t in batch['tasks'] if t['id'] in request['skip']]
                for target in [batch, *targets]:
                    if request.get('adopt'):
                        target.pop('held_checkpoint', None)
                    reset_recovery(target)
            elif batch.get('recovery_stop') and not retry_final:
                emit('progress', message='Returned the saved recovery stop. No new repair ran. '
                     'Inspect the recovery diagnosis before retrying or replanning.')
                return session.view(state)
            if batch.get('harness_phase') != 'adjudication':
                models.preflight(session, state, issue_harness=is_harness)
            if retry_final:
                batch.pop('recovery_planning', None)
                reset_recovery(batch)
            preserve_ready = False
            batch.pop('preflight_error', None)
            if is_harness:
                from . import issue_harness
                try:
                    return issue_harness.execute(session, state, batch, remote)
                finally:
                    completing = bool(batch.get('commit_execution'))
            for control in ('adopt', 'replan', 'skip', 'retry_final_recovery'):
                if request.get(control) and batch.get('execution_trace_version') == 2:
                    batch['trace_candidate'] = session_trace.decision(control, sources=[batch.get('trace_candidate')],
                        summary=str(request[control]), key=batch['checkpoint']['tree'])
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
                batch["discussion_stale"] = False
                active = active_feedback_keys(batch)
                batch["fingerprints"] = {i["key"]: i["fingerprint"] for i in session.items(state, active)} if active else {}
            batch["status"] = "running"
            batch.pop("formatter_error", None)
            batch.pop("error", None)
            session.save(state)
            for index, task in enumerate(batch["tasks"]):
                if task["status"] in ("complete", "skipped"):
                    continue
                statuses = {t["id"]: t["status"] for t in batch["tasks"]}
                if any(statuses[d] != "complete" for d in task["depends_on"]):
                    task["status"] = "blocked"
                    session.save(state)
                    continue
                check_fresh(session, state, batch)
                emit("progress", message=f"Owner task {task['id']}: {task['rationale']}")
                if task.get('recovery_stop'):
                    hold_task(session, state, batch, task, batch['tasks'][index + 1:])
                    continue
                if not run_task(session, state, batch, task):
                    if automatic(batch):
                        task.setdefault('recovery_stop', 'Implementation needs attention: ' + diagnosis(task['implementation']))
                        hold_task(session, state, batch, task, batch['tasks'][index + 1:])
                        continue
                    batch["status"] = "paused"
                    session.save(state)
                    return session.view(state)
            check_fresh(session, state, batch)
            stops = [t['id'] + ' · ' + t['recovery_stop'] for t in batch['tasks'] if t.get('recovery_stop')]
            if stops:
                batch.update(status='paused', recovery_stop='\n'.join(stops), error='\n'.join(stops))
                session.save(state)
                return session.view(state)
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
                    code = getattr(exc, "code", None)
                    message = str(exc)
                    if code == "formatter_failed":
                        batch["formatter_error"] = message
                    if automatic(batch) and not is_harness and (code == 'invalid_state' or
                            isinstance(exc, (AssertionError, KeyError, TypeError, ValueError))):
                        message = f'Internal Owner error ({type(exc).__name__}): {exc}. Inspect the operation log.'
                        emit('progress', message=message)
                    batch.update(status="interrupted" if code == "cancelled" else "paused", error=message)
                session.save(state)
            raise
