"""Conversation identity, assessment freshness and explicit thread-state changes."""
import copy
import json

from .common import ApiError, command, digest, now, read_json


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


def graphql(session, query, variables):
    host = session.manifest["repository"].split("/", 1)[0]
    response = command(["gh", "api", "--hostname", host, "graphql", "--input", "-"],
                       input_text=json.dumps({"query": query, "variables": variables}))
    try:
        value = json.loads(response.stdout)
        if value.get("errors") or not isinstance(value.get("data"), dict):
            raise ValueError("Missing GraphQL data")
        return value["data"]
    except (ValueError, AttributeError) as exc:
        raise ApiError("github_error", "GitHub could not confirm the conversation operation.") from exc


def thread_state(session, state, request):
    from .publication import refresh
    key, desired = request.get("thread"), request.get("resolved")
    if not isinstance(key, str) or not key.startswith("thread:") or type(desired) is not bool:
        raise ApiError("invalid_request", "Specify a thread key and a boolean resolved state.")
    host = session.manifest["repository"].split("/", 1)[0]
    viewer = command(["gh", "api", "--hostname", host, "user", "--jq", ".login"]).stdout.strip()
    if viewer.lower() != session.manifest["viewer"].lower():
        raise ApiError("account_changed", "GitHub account changed; reopen a review for this account.")
    refresh(session, state)
    thread = next((t for t in state["discussion"]["items"] if t["key"] == key and t["kind"] == "thread"), None)
    def result(changed=False, reason="", **extra):
        return {"changed": changed, "reason": reason, "review": session.view(state), **extra}
    if state["checked"]["pr"].get("state") != "OPEN":
        return result(reason="The PR is no longer open.")
    if not thread:
        return result(reason="Conversation is no longer available.")
    if thread["is_resolved"] == desired:
        return result(reason="Conversation already has the requested state.")
    if request.get("expected") != expected_context(session, state, thread):
        return result(reason="Code or conversation changed. Inspect the refreshed context and try again.")
    if thread.get("can_resolve" if desired else "can_unresolve") is not True:
        return result(reason="GitHub does not permit this conversation action for your account.")
    operation = "resolveReviewThread" if desired else "unresolveReviewThread"
    query = f"mutation($id: ID!) {{ {operation}(input: {{threadId: $id}}) {{ thread {{ id isResolved }} }} }}"
    uncertain = False
    try:
        data = graphql(session, query, {"id": key.removeprefix("thread:")})
        observed = data[operation]["thread"]
        if not isinstance(observed, dict) or observed.get("id") != key.removeprefix("thread:") or observed.get("isResolved") is not desired:
            raise ApiError("github_error", "The returned conversation state could not be confirmed.")
    except (ApiError, KeyError, TypeError):
        uncertain = True
    try:
        refresh(session, state)
    except ApiError:
        return result(reason="Conversation action sent; refresh GitHub to verify its current state. No retry was sent.",
                      uncertain=True)
    observed = next((t for t in state["discussion"]["items"] if t["key"] == key), {})
    confirmed = observed.get("is_resolved") is desired
    state["last_thread_action"] = {"thread": key, "resolved": desired, "at": now(),
                                   "observed": confirmed, "response_uncertain": uncertain}
    session.save(state)
    return result(changed=confirmed, uncertain=uncertain and not confirmed,
                  reason=("GitHub now shows the conversation as " + ("resolved." if desired else "unresolved."))
                  if confirmed else "The requested state could not be verified. Inspect GitHub before trying again.")
