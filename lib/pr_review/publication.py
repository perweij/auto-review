"""Exact-draft publication with durable intent and per-comment recovery receipts."""
import copy
import uuid

from .common import ApiError, atomic_json, digest, git, now, read_json
from . import workflow


def checkout(session):
    result = git(["--git-dir", session.manifest["git_common_dir"], "worktree", "list", "--porcelain"]).stdout
    return result.splitlines()[0].removeprefix("worktree ")


def repository_operation(session, operation, verdict="COMMENT"):
    from . import discussion
    from .repository import Repository
    if operation == "refresh":
        return discussion.refresh(session)
    if operation == "validate":
        return Repository.from_manifest(session.manifest).validate_publication(session.path, verdict)
    raise ApiError("invalid_request", "Unknown repository operation")


def gh(session, endpoint, payload=None, *, pages=False, resource="pulls"):
    from .forge_github_publication import gh as github_gh
    return github_gh(session, endpoint, payload, pages=pages, resource=resource)


def refresh(session, state):
    repository_operation(session, "refresh")
    session.manifest = read_json(session.path / "manifest.json")
    discussion = session.discussion()
    old_hash = state.get("discussion_hash")
    new_hash = session.manifest.get("discussion", {}).get("hash")
    pr = read_json(session.path / "publication/pr-latest.json", {})
    state["checked"] = {"at": now(), "head": pr.get("headRefOid"), "pr": pr}
    affected = workflow.affected_drafts(state["discussion"], discussion, state["comments"]) if old_hash != new_hash else []
    state["discussion"], state["discussion_hash"] = discussion, new_hash
    if old_hash != new_hash:
        for comment in state["comments"]:
            if comment["id"] in affected:
                comment["status"] = "unstaged"
                comment["needs_attention"] = "Discussion changed; inspect it and acknowledge this comment before staging again."
    state.pop("prepared", None)
    session.save(state)
    return affected


def marker(session, identity):
    from .forge_github_publication import marker as github_marker
    return github_marker(session, identity)


def with_marker(body, value):
    from .forge_github_publication import with_marker as github_with_marker
    return github_with_marker(body, value)


def make_actions(session, state):
    selected = copy.deepcopy([c for c in state['comments'] if c['status'] == 'staged'])
    adapter = publication_adapter(session)
    if sum(c['placement']['kind'] == 'review' for c in selected) > 1:
        raise ApiError('multiple_assessments', 'Stage one overall assessment per review.')
    for comment in selected:
        if comment.get('suggestion') and not adapter.suggestions:
            raise ApiError('unsupported_operation', 'This forge does not support code suggestions.')
        if comment['placement']['kind'] not in adapter.placements:
            raise ApiError('unsupported_operation', 'Unsupported publication placement.')
        if comment['placement']['kind'] == 'inline':
            comment['placement'] = session.git.anchor(comment['placement'])
    intent = {key: state[key] for key in ('verdict', 'verdict_submitted')}
    intent.update(comments=selected, verdict_requested=state.get('verdict_requested', False))
    return adapter.prepare(session, intent)


def persist_action(session, state, action):
    # The draft ledger owns both intent and receipts. Legacy actions.json is
    # retained unchanged as an import source, never a competing writable state.
    session.save(state)
    from .receipts import has_confirmed
    ledger = {a['id']: a for batch in state['batches'] for a in batch['actions']}.values()
    manifest = read_json(session.path / "manifest.json")
    count = sum(a['status'] == 'published' for a in ledger)
    remaining = any(a['status'] in ('draft', 'failed', 'submitting', 'ambiguous') for a in ledger)
    confirmed = any(has_confirmed(a) for a in ledger)
    manifest['publication'] = {'published_actions': count,
                               'status': 'partial' if confirmed and remaining else 'published' if count else 'not_published'}
    atomic_json(session.path / "manifest.json", manifest)


def valid_receipt(session, action, value):
    from .forge_github_publication import valid_receipt as github_valid_receipt
    return github_valid_receipt(session, action, value)


def mark_published(session, state, action, receipt, comment_results=None):
    action["result"] = receipt
    persist_action(session, state, action)
    if comment_results is None:
        comment_results = publication_adapter(session).comments(session, action, receipt, state)
    action.update(status="published", comment_results=comment_results)
    for c in state["comments"]:
        if c["id"] in action["comment_ids"]:
            c["status"] = "published"
            c["receipt"] = comment_results.get(c["id"], receipt)
    if action["kind"] == "review":
        state["verdict_submitted"] = True
        state["verdict_requested"] = False
        state["last_verdict"] = {"event": action["event"], "receipt": receipt}
        state["verdict"] = "COMMENT"
    persist_action(session, state, action)


def recover(session, state):
    from .forge import restore, require
    require(restore(session.manifest).forge, "publication")
    for batch in state["batches"]:
        for action in batch["actions"]:
            if action["status"] not in ("submitting", "ambiguous", "failed"):
                continue
            for operation in action.get('operations', [action]):
                if operation['status'] == 'published':
                    continue
                if operation['status'] == 'draft' and operation is not action:
                    continue
                matches = publication_adapter(session).lookup(session, operation)
                if len(matches) > 1:
                    raise ApiError('duplicate_receipts', 'Multiple posts match this operation; inspect before continuing.')
                if matches:
                    verify_operation(session, state, action, operation, matches[0])
                elif operation.get('result'):
                    raise ApiError('incomplete_receipts', 'Submitted operation cannot currently be verified; no retry sent.')
                else:
                    operation['status'] = 'superseded' if action.get('legacy') else 'failed'
                    action['status'] = operation['status']
                    persist_action(session, state, action)
            settle_action(action)
            persist_action(session, state, action)
            complete_action(session, state, action)


def prepare(session, state):
    from .forge import restore, require
    require(restore(session.manifest).forge, "publication")
    recover(session, state)
    affected = refresh(session, state)
    try:
        repository_operation(session, "validate", state["verdict"] if not state["verdict_submitted"] else "COMMENT")
    except ApiError as exc:
        if exc.code == "cancelled":
            raise
        for c in state["comments"]:
            if c["status"] == "staged":
                c["status"] = "unstaged"
                c["needs_attention"] = str(exc)
                affected.append(c["id"])
        session.save(state)
        return {"ready": False, "reason": str(exc), "affected": affected, "review": session.view(state)}
    if affected:
        return {"ready": False, "reason": "Inspect changed discussion before staging again.",
                "affected": affected, "review": session.view(state)}
    for c in state["comments"]:
        if c["status"] == "staged" and session.blockers(c, state):
            c["status"] = "unstaged"
            affected.append(c["id"])
    if affected:
        session.save(state)
        return {"ready": False, "reason": "Some comments need attention; inspect their placement and suggestion blockers.",
                "affected": affected, "review": session.view(state)}
    actions = make_actions(session, state)
    if not actions:
        return {"ready": False, "reason": "Nothing is staged and no new verdict is selected.", "review": session.view(state)}
    # Retain the marker for an unchanged failed submission. A full lookup has
    # just checked it again, and retries remain identifiable after interruptions.
    for index, action in enumerate(actions):
        for previous in (a for b in reversed(state["batches"]) for a in b["actions"]):
            if previous["status"] != "failed" or any(previous.get(k) != action.get(k) for k in
                                                       ("kind", "target", "event", "comment_ids")):
                continue
            if publication_adapter(session).retry_equivalent(previous, action):
                actions[index] = copy.deepcopy(previous)
                actions[index]["status"] = "draft"
                break
    from .receipts import has_confirmed
    for previous in (a for b in state['batches'] for a in b['actions']):
        if previous['status'] != 'published' and has_confirmed(previous):
            if not any(a['id'] == previous['id'] for a in actions):
                return {'ready': False, 'reason': f"Partially published action {previous['id']} (comments: {', '.join(previous['comment_ids'])}) must resume with unchanged text, destinations and verdict. Inspect, acknowledge and restage those comments.", 'review': session.view(state)}
    batch = {"id": uuid.uuid4().hex, "actions": actions, "created_at": now(),
             "review_status": workflow.status_fingerprint(session)}
    state["prepared"] = batch
    session.save(state)
    counts = {"inline": sum(c["status"] == "staged" and c["placement"]["kind"] == "inline" for c in state["comments"]),
              "replies": sum(a["kind"] == "reply" for a in actions),
              "overall": sum(c["status"] == "staged" and c["placement"]["kind"] == "review" for c in state["comments"])}
    warnings = ([f"{state['blocking_findings']} retained blocking findings remain."]
                if state.get("blocking_findings", 0) and any(a["event"] == "APPROVE" for a in actions) else [])
    if session.manifest.get("analysis_kind") == "followup":
        warnings.append(f"Limited coverage: only {len(session.manifest.get('followup_threads', []))} selected conversations were assessed; the whole PR was not reviewed.")
    return {"ready": True, "confirmation": digest(batch), "counts": counts,
            "verdict": next((a["event"] for a in actions if a["kind"] == "review"), None),
            "warnings": warnings,
            "review": session.view(state)}


def publish(session, state, request):
    from .forge import restore, require
    require(restore(session.manifest).forge, "publication")
    batch = state.get("prepared")
    if not batch or request.get("confirmation") != digest(batch):
        raise ApiError("confirmation_expired", "The staged batch changed; prepare and confirm it again.")
    # Recover before checking freshness: an earlier lost response is own activity.
    recover(session, state)
    if any(c["status"] != "staged" for c in state["comments"]
           if any(c["id"] in a["comment_ids"] for a in batch["actions"])) or (
               state["verdict_submitted"] and any(a["kind"] == "review" and a["event"] != "COMMENT" for a in batch["actions"])):
        state.pop("prepared", None)
        session.save(state)
        return {"complete": False, "reason": "Earlier submission was recovered; prepare only the remaining comments.",
                "review": session.view(state)}
    if refresh(session, state):
        return {"complete": False, "reason": "Discussion changed after confirmation.", "review": session.view(state)}
    if batch.get("review_status") != workflow.status_fingerprint(session):
        return {"complete": False, "reason": "GitHub review status changed. Inspect it and prepare publication again.",
                "review": session.view(state)}
    repository_operation(session, "validate", next((a["event"] for a in batch["actions"] if a["kind"] == "review"), "COMMENT"))
    if any(session.blockers(c, state) for c in state['comments'] if c['status'] == 'staged'):
        return {'complete': False, 'reason': 'Draft or suggestion verification changed; inspect and prepare again.',
                'review': session.view(state)}
    from .receipts import has_confirmed
    previous_actions = [a for b in state['batches'] for a in b['actions']]
    resumed = set()
    for index, action in enumerate(batch['actions']):
        original = next((a for a in previous_actions if a['id'] == action['id'] and has_confirmed(a)), None)
        if original is not None:
            batch['actions'][index] = original
            resumed.add(original['id'])
    for previous in previous_actions:
        if previous['status'] in ('draft', 'failed') and not has_confirmed(previous):
            previous['status'] = 'superseded'
            for operation in previous.get('operations', []):
                if operation['status'] in ('draft', 'failed'):
                    operation['status'] = 'superseded'
    state['batches'].append({**batch, 'actions': [a for a in batch['actions'] if a['id'] not in resumed],
                             'resumed_actions': sorted(resumed)})
    session.save(state)
    for action in batch["actions"]:
        # Recheck between network writes. Confirmed own receipts are filtered by
        # the shared discussion collector, including implicit review wrappers.
        if refresh(session, state):
            return {"complete": False, "reason": "Discussion changed; unsent comments need inspection.", "review": session.view(state)}
        if not any(op["status"] == "published" and op["kind"] == "review" for a in batch["actions"] for op in a.get("operations", [a])) and batch.get("review_status") != workflow.status_fingerprint(session):
            return {"complete": False, "reason": "GitHub review status changed; prepare the remaining publication again.",
                    "review": session.view(state)}
        repository_operation(session, "validate", action["event"])
        from .suggestions import blockers as suggestion_blockers
        if any(suggestion_blockers(session, c, state) for c in state['comments'] if c['id'] in action['comment_ids']):
            return {'complete': False, 'reason': 'Suggestion verification changed; inspect the unsent comments.',
                    'review': session.view(state)}
        sent_here = False
        for operation in action.get('operations', [action]):
            if operation['status'] == 'published':
                continue
            if sent_here:
                if refresh(session, state):
                    return {'complete': False, 'reason': 'Discussion changed; inspect remaining operations.', 'review': session.view(state)}
                own_verdict = any(op['status'] == 'published' and op['kind'] == 'review' for a in batch['actions'] for op in a.get('operations', [a]))
                if not own_verdict and batch.get('review_status') != workflow.status_fingerprint(session):
                    return {'complete': False, 'reason': 'Review status changed; prepare remaining operations again.', 'review': session.view(state)}
                repository_operation(session, 'validate', action['event'])
            operation['status'] = action['status'] = 'submitting'
            persist_action(session, state, action)
            try:
                receipt = publication_adapter(session).submit(session, operation)
                if not publication_adapter(session).matches(session, operation, receipt):
                    raise ApiError('invalid_response', 'Submission response could not be verified.')
                verify_operation(session, state, action, operation, receipt)
                sent_here = True
            except ApiError as exc:
                operation['status'] = action['status'] = 'ambiguous'
                persist_action(session, state, action)
                if exc.code == 'cancelled':
                    raise
                return {'complete': False, 'reason': str(exc), 'needs_recovery': True, 'review': session.view(state)}
        complete_action(session, state, action)
    return {"complete": True, "review": session.view(state)}


def publication_adapter(session):
    from .forge import adapter, restore, require
    project = restore(session.manifest)
    require(project.forge, 'publication')
    return adapter(project.forge).publication


def verify_operation(session, state, action, operation, receipt):
    operation['result'] = receipt
    operation['receipt_verified'] = True
    operation['evidence'] = publication_adapter(session).evidence(operation)
    persist_action(session, state, action)
    operation['comment_results'] = publication_adapter(session).comments(session, operation, receipt, state)
    for identity in operation.get('comment_ids', []):
        operation['comment_results'].setdefault(identity, receipt)
    operation['status'] = 'published'
    operation['evidence'] = publication_adapter(session).evidence(operation)
    settle_action(action)
    persist_action(session, state, action)


def complete_action(session, state, action):
    operations = action.get('operations', [action])
    if all(op['status'] == 'published' for op in operations):
        comments = {key: value for op in operations for key, value in op.get('comment_results', {}).items()}
        verdict = next((op for op in operations if op.get('kind') == 'review' and op.get('event') == action.get('event')), operations[-1])
        mark_published(session, state, action, verdict['result'], comments)


def render_comment(session, comment):
    from .forge import adapter, restore
    forge = adapter(restore(session.manifest).forge)
    if 'publication' in forge.capabilities:
        return forge.publication.render(comment)
    from .suggestions import render
    return render(comment)


def settle_action(action):
    if 'operations' not in action:
        return
    statuses = {op['status'] for op in action['operations']}
    # Keep all-published actions eligible for final comment/verdict bookkeeping.
    action['status'] = 'ambiguous' if statuses & {'submitting', 'ambiguous'} else 'failed'
