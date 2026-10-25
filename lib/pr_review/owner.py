"""Owner application use cases shared by the terminal and protocol adapters."""
import copy
import uuid
from contextlib import nullcontext

from .common import ApiError, locked, require_text
from .owner_state import OwnerSession, open_session, discover, toolkit, refresh, validate_settings
from . import owner_git, owner_fixes, owner_publication, owner_commit


COMMANDS = ["owner-open", "owner-import", "owner-discover", "owner-read", "owner-refresh", "owner-mutate", "owner-fix",
            "owner-diff", "owner-prepare-commit", "owner-associate", "owner-prepare", "owner-publish", "owner-recover", "owner-thread-state", "toolkit"]


def dispatch(request):
    name = request["command"]
    if name == "owner-open":
        return open_session(request)
    if name == "owner-import":
        from .owner_import import from_review
        return from_review(request)
    if name == "owner-discover":
        return discover(request)
    if name == "toolkit":
        return toolkit(request)
    session = OwnerSession(request)
    if name == "owner-read":
        return session.view()
    if name == "owner-diff":
        with locked(session.execution_lock) if request.get("current") else nullcontext():
            state = session.load()
            batch = next((b for b in state["batches"] if b["id"] == request.get("batch")), None)
            if not batch:
                raise ApiError("invalid_request", "Choose a batch to inspect.")
            task = next((t for t in batch["tasks"] if t["id"] == request.get("task")), None)
            before = task["before"]["tree"] if task and task.get("before") else batch["baseline"]
            after = task.get("after", batch["checkpoint"])["tree"] if task else batch["checkpoint"]["tree"]
            if request.get("current"):
                session.check(request, state)
                before = batch["checkpoint"]["tree"]
                after = owner_git.checkpoint(session, state, "inspect current partial changes")["tree"]
            return {"diff": owner_git.diff(session, before, after), "before": before, "after": after}
    if name not in COMMANDS:
        raise ApiError("invalid_request", "Unknown owner command.")
    with locked(session.execution_lock):
        state = session.load()
        session.check(request, state)
        if name == "owner-fix":
            return owner_fixes.execute(session, state, request)
        if name == "owner-refresh":
            refresh(session, state)
            if any(r.get("commits") and r["status"] != "published" for r in state["replies"]):
                owner_publication.invalidate(session, state, owner_git.fetch_head(session, state))
                session.save(state)
            return session.view(state)
        if name == "owner-mutate":
            return mutate(session, state, request)
        if name == "owner-associate":
            return owner_publication.associate(session, state, request)
        if name == "owner-prepare-commit":
            return owner_commit.prepare(session, state, request)
        with locked(session.repo.lock):
            if name == "owner-thread-state":
                return thread_state(session, state, request)
            if name == "owner-prepare":
                return owner_publication.prepare(session, state)
            if name == "owner-publish":
                return owner_publication.publish(session, state, request)
            if name == "owner-recover":
                return owner_publication.recover(session, state)
    raise ApiError("invalid_request", "Unknown owner command.")


def mutate(session, state, request):
    action = request.get("operation")
    if action == "settings":
        state["settings"] = validate_settings(request.get("settings"))
    elif action in ("acknowledge", "defer", "restore", "handled"):
        for item in session.items(state, request.get("items")):
            if action == "acknowledge":
                item["unread"] = False
            else:
                item["triage"] = {"defer": "deferred", "restore": "attention", "handled": "handled"}[action]
                item["queued"] = action == "restore"
            item["attention"] = item["triage"] == "attention" and bool(item.get("queued") or item["unread"] or item.get("change_requested") or
                (item["kind"] == "thread" and not item["is_resolved"]))
    elif action == "reply":
        item = session.items(state, [request.get("item")])[0]
        body = require_text(request.get("body"), "reply body")
        state["replies"].append(owner_publication.new_reply(item, body, human_edited=True))
    elif action in ("edit", "stage", "unstage", "acknowledge-reply"):
        ids = request.get("replies") or [request.get("reply")]
        if not isinstance(ids, list) or any(not isinstance(k, str) for k in ids) or len(set(ids)) != len(ids):
            raise ApiError("invalid_request", "Choose distinct replies.")
        pending = {a["reply"] for a in state["actions"] if a["status"] in ("ambiguous", "submitting")}
        for identity in ids:
            reply = next((r for r in state["replies"] if r["id"] == identity), None)
            if not reply or reply["status"] == "published" or identity in pending:
                raise ApiError("immutable_reply", "Choose an unpublished reply without an uncertain submission.")
            if action == "edit":
                body = require_text(request.get("body"), "reply body")
                if body != reply["body"]:
                    reply["versions"].append(reply["body"])
                    reply.update(body=body, status="unstaged", human_edited=True)
            elif action == "acknowledge-reply":
                item = session.items(state, [reply["source"]])[0]
                reply["fingerprint"] = item["fingerprint"]
                reply.pop("needs_attention", None)
                reply["status"] = "unstaged"
            elif action == "stage":
                if not state.get("metadata"):
                    raise ApiError("refresh_required", "Refresh GitHub before staging replies.")
                owner_publication.blockers(session, state, reply, state["metadata"]["headRefOid"])
                reply["status"] = "staged"
            else:
                reply["status"] = "unstaged"
    else:
        raise ApiError("invalid_request", "Unknown owner mutation.")
    state.pop("prepared", None)
    session.save(state)
    return session.view(state)


def thread_state(session, state, request):
    item = copy.deepcopy(session.items(state, [request.get("item")])[0])
    desired = request.get("resolved")
    if item["kind"] != "thread" or type(desired) is not bool:
        raise ApiError("invalid_request", "Choose a thread and resolution state.")
    refresh(session, state)
    fresh = session.items(state, [item["key"]])[0]
    if fresh["fingerprint"] != item["fingerprint"] or fresh["is_resolved"] != item["is_resolved"]:
        raise ApiError("stale_discussion", "Thread changed; inspect refreshed discussion and invoke again.")
    if not fresh.get("can_resolve" if desired else "can_unresolve"):
        raise ApiError("permission_denied", "GitHub does not permit this thread action.")
    verb = "resolveReviewThread" if desired else "unresolveReviewThread"
    query = f"mutation($id: ID!) {{ {verb}(input: {{threadId: $id}}) {{ thread {{ id isResolved }} }} }}"
    state["thread_intent"] = {"item": item["key"], "resolved": desired, "id": uuid.uuid4().hex}
    session.save(state)
    try:
        session.repo.graphql(query, id=item["key"].split(":", 1)[1])
    except ApiError:
        refresh(session, state)
        if session.items(state, [item["key"]])[0]["is_resolved"] != desired:
            raise ApiError("uncertain_thread_state", "Thread action was not confirmed; inspect GitHub before invoking again.")
    else:
        refresh(session, state)
        if session.items(state, [item["key"]])[0]["is_resolved"] != desired:
            raise ApiError("uncertain_thread_state", "Thread action has not been confirmed by read-back.")
    state.pop("thread_intent", None)
    session.save(state)
    return session.view(state)
