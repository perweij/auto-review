"""Conversation identity, assessment freshness and explicit thread-state changes."""
import copy

from .common import ApiError, digest, now, read_json


def thread_fingerprint(thread):
    # Permission and resolver metadata are not discussion content. Include code
    # anchors so moving/outdating a thread still requires a fresh inspection.
    return digest({k: thread.get(k) for k in
                   ("key", "root_id", "comments", "path", "line", "original_line", "is_outdated", "is_resolved")})


def content_fingerprint(item):
    return digest({"key": item.get("key"), "comments": item.get("comments", [])})


def affected_drafts(old, new, comments):
    before = {i["key"]: i for i in old.get("items", [])}
    after = {i["key"]: i for i in new.get("items", [])}
    changed = {k for k in before.keys() | after.keys()
               if thread_fingerprint(before.get(k, {})) != thread_fingerprint(after.get(k, {}))}
    content_changed = any(content_fingerprint(before.get(k, {})) != content_fingerprint(after.get(k, {}))
                          for k in before.keys() | after.keys())
    affected = []
    for c in comments:
        if c["status"] != "staged":
            continue
        place = c.get("placement", {})
        dependencies = set(c.get("discussion_keys", []))
        if place.get("kind") == "reply":
            dependencies.add(place.get("key"))
        if dependencies & changed or (place.get("kind") != "reply" and content_changed):
            affected.append(c["id"])
    return affected


def status(session):
    return read_json(session.path / "discussion/review-status.json", {"available": False})


def status_fingerprint(session):
    value = status(session)
    opinion = value.get("opinionated")
    if opinion and opinion.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
        opinion = None
    # Posting replies creates implicit COMMENTED reviews. These are activity,
    # not a new opinion, and must not interrupt the rest of our own batch.
    return digest({"available": value.get("available"), "decision": value.get("decision"), "opinionated": opinion})


def assessments(session, state):
    report = read_json(session.path / "reviews/followup.json", {})
    inputs = read_json(session.path / "reviews/followup-input.json", {})
    current = {t["key"]: t for t in state.get("discussion", {}).get("items", [])}
    head = (state.get("checked") or {}).get("head", session.git.head)
    result = copy.deepcopy(report.get("assessments", []))
    for item in result:
        key = item["thread_key"]
        item["head"] = inputs.get("head")
        item["stale"] = (head != inputs.get("head") or key not in current or
                         inputs.get("fingerprints", {}).get(key) != thread_fingerprint(current[key]))
    return result


def expected_context(session, state, thread):
    return {"head": (state.get("checked") or {}).get("head", session.git.head),
            "thread": thread_fingerprint(thread)}


def thread_state(session, state, request):
    from .publication import refresh
    key, desired = request.get("thread"), request.get("resolved")
    if not isinstance(key, str) or not key.startswith("thread:") or type(desired) is not bool:
        raise ApiError("invalid_request", "Specify a thread key and a boolean resolved state.")
    from .forge import adapter, restore, check_account
    forge = adapter(restore(session.manifest).forge)
    from .publication import checkout
    account = forge.account(restore(session.manifest), checkout(session))
    check_account(session.manifest.get('forge_account'), account, session.manifest['viewer'])
    refresh(session, state)
    thread = next((t for t in state["discussion"]["items"] if t["key"] == key and t["kind"] == "thread"), None)
    def result(changed=False, reason="", **extra):
        return {"changed": changed, "reason": reason, "review": session.view(state), **extra}
    if state["checked"]["pr"].get("state") != "OPEN":
        return result(reason="The PR is no longer open.")
    if not thread:
        return result(reason="Conversation is no longer available.")
    if thread["is_resolved"] == desired:
        from .conversations import accept_observed
        accept_observed(state, thread)
        session.save(state)
        return result(reason="Conversation already has the requested state.")
    if request.get("expected") != expected_context(session, state, thread):
        return result(reason="Code or conversation changed. Inspect the refreshed context and try again.")
    if thread.get("can_resolve" if desired else "can_unresolve") is not True:
        return result(reason="GitHub does not permit this conversation action for your account.")
    from .conversations import change
    try:
        confirmed, uncertain = change(session, state, thread, desired, lambda: refresh(session, state),
                                      lambda: next((t for t in state['discussion']['items'] if t['key'] == key), None))
    except ApiError as exc:
        if exc.code != 'uncertain_thread_state':
            raise
        return result(reason=str(exc), uncertain=True)
    state["last_thread_action"] = {"thread": key, "resolved": desired, "at": now(),
                                   "observed": confirmed, "response_uncertain": uncertain}
    session.save(state)
    return result(changed=confirmed, uncertain=uncertain and not confirmed,
                  reason=("GitHub now shows the conversation as " + ("resolved." if desired else "unresolved."))
                  if confirmed else "The requested state could not be verified. Inspect GitHub before trying again.")
