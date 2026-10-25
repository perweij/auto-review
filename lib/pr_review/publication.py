"""Exact-draft publication with durable intent and per-comment recovery receipts."""
import copy
import json
import uuid

from .common import ApiError, atomic_json, command, digest, git, now, read_json
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
    host, repo = session.manifest["repository"].split("/", 1)
    args = ["gh", "api", "--hostname", host]
    if pages:
        args += ["--paginate", "--slurp"]
    if payload is not None:
        args += ["-X", "POST", "--input", "-"]
    if resource not in ("pulls", "issues"):
        raise ApiError("invalid_request", "Invalid GitHub comment resource.")
    result = command([*args, f"repos/{repo}/{resource}/{session.manifest['pr']}/{endpoint}"],
                     input_text=json.dumps(payload) if payload is not None else None)
    try:
        value = json.loads(result.stdout)
        if pages:
            if not isinstance(value, list) or any(not isinstance(p, list) for p in value):
                raise ValueError("Invalid pagination")
            value = [item for page in value for item in page]
        return value
    except ValueError as exc:
        raise ApiError("invalid_response", "GitHub returned incomplete publication data; resolve before retrying.") from exc


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
    return f"<!-- pr-review generation={session.id} head={session.git.head} action={identity} -->"


def with_marker(body, value):
    return body + "\n\n" + value


def make_actions(session, state):
    selected = [c for c in state["comments"] if c["status"] == "staged"]
    actions = []
    review = [c for c in selected if c["placement"]["kind"] in ("inline", "review")]
    overall = [c for c in review if c["placement"]["kind"] == "review"]
    if len(overall) > 1:
        raise ApiError("multiple_assessments", "Stage one overall assessment per review; inline comments remain separate.")
    for c in selected:
        if c["placement"]["kind"] != "reply":
            continue
        identity = "api-" + uuid.uuid4().hex
        actions.append({"id": identity, "kind": "reply", "status": "draft", "comment_ids": [c["id"]],
                        "finding_ids": c["finding_ids"], "target": copy.deepcopy(c["placement"]), "event": "COMMENT",
                        "marker": marker(session, identity), "viewer": session.manifest["viewer"],
                        "payload": {"body": with_marker(c["body"], marker(session, identity))}})
    # Staged comments use COMMENT unless the user explicitly chose a verdict.
    # Sending a verdict on its own also requires an explicit user choice.
    if review or (state.get("verdict_requested") and not state["verdict_submitted"]):
        identity = "api-" + uuid.uuid4().hex
        comment_markers = {c["id"]: marker(session, identity + "/" + c["id"]) for c in review}
        payload = {"commit_id": session.git.head,
                   "event": state["verdict"] if state.get("verdict_requested") and not state["verdict_submitted"] else "COMMENT",
                   "body": with_marker(overall[0]["body"] if overall else "", marker(session, identity))}
        inline = []
        for c in review:
            if c["placement"]["kind"] != "inline":
                continue
            a = session.git.anchor(c["placement"])
            value = {"path": a["path"], "line": a["line"], "side": a["side"],
                     "body": with_marker(c["body"], comment_markers[c["id"]])}
            if a["start_line"] != a["line"]:
                value.update(start_line=a["start_line"], start_side=a["side"])
            inline.append(value)
        if inline:
            payload["comments"] = inline
        actions.append({"id": identity, "kind": "review", "status": "draft", "comment_ids": [c["id"] for c in review],
                        "finding_ids": [f for c in review for f in c["finding_ids"]], "target": None,
                        "event": payload["event"], "marker": marker(session, identity), "comment_markers": comment_markers,
                        "viewer": session.manifest["viewer"], "payload": payload})
    return actions


def persist_action(session, state, action):
    # The draft ledger owns both intent and receipts. Legacy actions.json is
    # retained unchanged as an import source, never a competing writable state.
    session.save(state)
    ledger = [a for batch in state['batches'] for a in batch['actions']]
    statuses = {a['id']: a['status'] for a in ledger}
    manifest = read_json(session.path / "manifest.json")
    count = sum(a.get("status") == "published" for a in ledger)
    remaining = any(status in ("draft", "failed", "submitting", "ambiguous") for status in statuses.values())
    manifest["publication"] = {"published_actions": count,
                               "status": "partial" if count and remaining else "published" if count else "not_published"}
    atomic_json(session.path / "manifest.json", manifest)


def valid_receipt(session, action, value):
    if (not isinstance(value, dict) or type(value.get("id")) is not int
            or not isinstance(value.get('user'), dict)
            or not isinstance(value['user'].get('login'), str)
            or not isinstance(value.get('body'), str)):
        return False
    if str((value.get("user") or {}).get("login", "")).lower() != str(action["viewer"]).lower():
        return False
    if action["marker"] not in (value.get("body") or ""):
        return False
    if action["kind"] == "timeline":
        url = value.get("html_url")
        expected = f"https://{session.manifest['repository']}/pull/{session.manifest['pr']}#issuecomment-{value['id']}"
        return isinstance(url, str) and url.lower() == expected.lower()
    if action["kind"] == "reply":
        return value.get("in_reply_to_id") == action["target"]["root_id"]
    expected = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}[action["event"]]
    return value.get("commit_id") == session.git.head and value.get("state", "").upper() == expected


def mark_published(session, state, action, receipt):
    action["result"] = receipt
    comment_results = {}
    if action["kind"] == "review" and action["payload"].get("comments"):
        values = gh(session, f"reviews/{receipt['id']}/comments", pages=True)
        if any(not isinstance(v, dict) or not isinstance(v.get('user'), dict)
               or not isinstance(v.get('body'), str) for v in values):
            raise ApiError('incomplete_receipts', 'Review submitted; inline comment receipts are incomplete. Resolve before retrying.')
        for identity, tracking in action.get("comment_markers", {}).items():
            value = next((v for v in values if tracking in (v.get("body") or "") and
                          v.get("pull_request_review_id") == receipt["id"] and
                          str((v.get("user") or {}).get("login", "")).lower() == str(action["viewer"]).lower()), None)
            if value:
                comment_results[identity] = value
        expected = [c["id"] for c in state["comments"] if c["id"] in action["comment_ids"] and c["placement"]["kind"] == "inline"]
        if any(i not in comment_results for i in expected):
            # Parent review exists. Keep intent ambiguous and never repost it;
            # the next recovery fetches its comment receipts again.
            persist_action(session, state, action)
            raise ApiError("incomplete_receipts", "Review submitted; waiting for its inline comment receipts. Resolve before retrying.")
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
    for batch in state["batches"]:
        for action in batch["actions"]:
            if action["status"] not in ("submitting", "ambiguous", "failed"):
                continue
            values = gh(session, "comments" if action["kind"] == "reply" else "reviews", pages=True)
            if any(not isinstance(v, dict) or type(v.get('id')) is not int or
                   not isinstance(v.get('user'), dict) or not isinstance(v['user'].get('login'), str) or
                   not isinstance(v.get('body'), str) for v in values):
                raise ApiError('invalid_response', 'Malformed publication lookup; refusing to risk a duplicate')
            matches = [v for v in values if valid_receipt(session, action, v)]
            if len(matches) > 1:
                raise ApiError("duplicate_receipts", "Multiple posts match this action; inspect GitHub before continuing.")
            if matches:
                mark_published(session, state, action, matches[0])
            elif action.get("result"):
                raise ApiError("incomplete_receipts", "A review was submitted but cannot currently be verified; no retry sent.")
            else:
                action["status"] = "superseded" if action.get("legacy") else "failed"
                persist_action(session, state, action)


def prepare(session, state):
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
        return {"ready": False, "reason": "Some comments need a corrected placement.",
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
            def without_markers(payload):
                import re
                return re.sub(r"<!-- pr-review [^>]+ -->", "", json.dumps(payload, sort_keys=True))
            if without_markers(previous["payload"]) == without_markers(action["payload"]):
                actions[index] = copy.deepcopy(previous)
                actions[index]["status"] = "draft"
                break
    batch = {"id": uuid.uuid4().hex, "actions": actions, "created_at": now(),
             "review_status": workflow.status_fingerprint(session)}
    state["prepared"] = batch
    session.save(state)
    counts = {"inline": sum(len(a["payload"].get("comments", [])) for a in actions),
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
    for previous in (a for b in state["batches"] for a in b["actions"]):
        if previous["status"] in ("draft", "failed"):
            previous["status"] = "superseded"
    state["batches"].append(batch)
    session.save(state)
    for action in batch["actions"]:
        # Recheck between network writes. Confirmed own receipts are filtered by
        # the shared discussion collector, including implicit review wrappers.
        if refresh(session, state):
            return {"complete": False, "reason": "Discussion changed; unsent comments need inspection.", "review": session.view(state)}
        if not any(a["status"] == "published" and a["kind"] == "review" for a in batch["actions"]) and batch.get("review_status") != workflow.status_fingerprint(session):
            return {"complete": False, "reason": "GitHub review status changed; prepare the remaining publication again.",
                    "review": session.view(state)}
        repository_operation(session, "validate", action["event"])
        action["status"] = "submitting"
        persist_action(session, state, action)
        try:
            endpoint = f"comments/{action['target']['root_id']}/replies" if action["kind"] == "reply" else "reviews"
            receipt = gh(session, endpoint, action["payload"])
            if not valid_receipt(session, action, receipt):
                raise ApiError("invalid_response", "Submission response could not be verified.")
            mark_published(session, state, action, receipt)
        except ApiError as exc:
            action["status"] = "ambiguous"
            persist_action(session, state, action)
            if exc.code == "cancelled":
                raise
            return {"complete": False, "reason": str(exc), "needs_recovery": True, "review": session.view(state)}
    return {"complete": True, "review": session.view(state)}
