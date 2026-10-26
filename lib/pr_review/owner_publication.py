"""Owner reply drafts and commit provenance using shared GitHub receipts."""
import copy
import uuid

from .common import ApiError, digest, now, require_text
from .publication import gh, valid_receipt, with_marker
from .owner_state import refresh
from . import owner_git as workspace, owner_commit_plan


def commit(session, value):
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ApiError("invalid_commit", "Choose a commit SHA or revision.")
    return session.repo.git("rev-parse", "--verify", "--end-of-options", value + "^{commit}").stdout.strip()


def fixed_items(batch):
    return {o["item"] for o in batch.get("review", {}).get("outcomes", []) if o["status"] == "fixed"}


def associate(session, state, request):
    refresh(session, state)
    remote = workspace.fetch_head(session, state)
    workspace.validate(session, state)
    local = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
    chosen = request.get("batch")
    if chosen and not any(b["id"] == chosen and b["status"] == "ready" for b in state["batches"]):
        raise ApiError("invalid_target", "Choose a batch with a successful combined review.")
    if request.get("commits") and not chosen:
        raise ApiError("invalid_request", "Explicit commit associations require a batch.")
    for batch in state["batches"]:
        if batch["status"] != "ready" or (chosen and chosen != batch["id"]):
            continue
        associations = batch.setdefault("associations", {})
        if chosen and request.get("commits"):
            values = request["commits"]
            if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
                raise ApiError("invalid_request", "Choose one or more commits.")
            commits = list(dict.fromkeys(commit(session, v) for v in values))
            if any(not workspace.ancestor(session, sha, local) and not workspace.ancestor(session, sha, remote) for sha in commits):
                raise ApiError("invalid_commit", "Associated commits must belong to the owner branch or current PR.")
            keys = request.get("items") or sorted(fixed_items(batch))
            session.items(state, keys)
            if not set(keys) <= fixed_items(batch):
                raise ApiError("invalid_target", "Only successfully verified code-fix outcomes can have fixed-in replies.")
            for key in keys:
                associations[key] = {"commits": commits, "method": "explicit", "at": now()}
        else:
            _plan, progress = owner_commit_plan.update(session, batch, local)
            if progress["items"]:
                for key in (k for k in batch["items"] if k in fixed_items(batch) and k in progress["items"]):
                    # A newly amended exact commit can replace an obsolete automatic association.
                    if key not in associations or associations[key]["method"] in ("exact", "task"):
                        associations[key] = {"commits": progress["items"][key],
                                             "method": "exact" if progress["method"] == "exact" else "task", "at": now()}
        for key, association in associations.items():
            association["refs"] = [workspace.pin_commit(session, sha) for sha in association["commits"]]
            item = session.items(state, [key])[0]
            if item["kind"] == "local_review":
                continue  # Local findings have provenance, but no GitHub reply destination.
            if not all(workspace.ancestor(session, sha, remote) for sha in association["commits"]):
                continue
            if not state["push_target"]["repository"]:
                raise ApiError("missing_head_repository", "GitHub no longer identifies the PR head repository; inspect the destination before generating commit links.")
            if item["fingerprint"] != batch["verified_fingerprints"].get(key):
                continue
            existing = next((r for r in state["replies"] if r.get("batch") == batch["id"] and r["source"] == key), None)
            body = "Fixed in " + ", ".join(f"[{sha[:12]}](https://{state['push_target']['repository']}/commit/{sha})" for sha in association["commits"]) + "."
            if item["kind"] != "thread":
                body = f"In response to [this feedback]({item['comments'][0]['url']}):\n\n" + body
            if existing:
                if existing["status"] != "published" and existing.get("commits") != association["commits"]:
                    existing.update(commits=association["commits"], status="unstaged", needs_attention="Commit association changed; inspect this reply.")
                    if not existing.get("human_edited"):
                        existing["body"] = body
                continue
            state["replies"].append(new_reply(item, body, batch=batch["id"], commits=association["commits"], status="staged"))
    invalidate(session, state, remote)
    state.pop("prepared", None)
    session.save(state)
    return session.view(state)


def new_reply(item, body, **extra):
    if item["kind"] == "local_review":
        raise ApiError("local_only", "This finding is local; choose a GitHub conversation to compose a reply.")
    return {"id": uuid.uuid4().hex, "source": item["key"], "fingerprint": item["fingerprint"],
            "target": {"kind": "reply", "root_id": item["root_id"]} if item["kind"] == "thread" else {"kind": "timeline"},
            "body": body, "status": "unstaged", "versions": [], "created_at": now(), **extra}


def invalidate(session, state, remote):
    items = {i["key"]: i for i in state["feedback"]}
    for reply in state["replies"]:
        if reply["status"] == "published":
            continue
        item = items.get(reply["source"])
        reason = None
        if not item or item.get("missing") or item["fingerprint"] != reply["fingerprint"]:
            reason = "Discussion changed; inspect and acknowledge this reply."
        elif reply.get("commits") and any(not workspace.ancestor(session, sha, remote) for sha in reply["commits"]):
            reason = "Associated commit is not on the current PR; push or reassociate before staging."
        if reason:
            reply.update(status="unstaged", needs_attention=reason)


def blockers(session, state, reply, remote):
    item = session.items(state, [reply["source"]])[0]
    if item["kind"] == "local_review":
        raise ApiError("local_only", "Local findings do not have a GitHub reply destination.")
    if reply.get("needs_attention") or item["fingerprint"] != reply["fingerprint"]:
        raise ApiError("stale_discussion", "Inspect and acknowledge the changed reply context first.")
    require_text(reply["body"], "reply body")
    if item["kind"] == "thread" and not item.get("can_reply"):
        raise ApiError("permission_denied", "GitHub does not permit replying to this thread.")
    if reply.get("commits") and any(not workspace.ancestor(session, sha, remote) for sha in reply["commits"]):
        raise ApiError("unpublished_commit", "The fix commits are not on the current PR head.")


def prepare(session, state):
    refresh(session, state)
    remote = workspace.fetch_head(session, state)
    invalidate(session, state, remote)
    session.save(state)
    if state["metadata"]["state"] != "OPEN":
        raise ApiError("closed_pr", "Publishing owner replies requires an open PR.")
    if any(a["status"] in ("submitting", "ambiguous") for a in state["actions"]):
        raise ApiError("uncertain_publication", "Recover uncertain replies before publishing again.")
    selected = [r for r in state["replies"] if r["status"] == "staged"]
    if not selected:
        raise ApiError("nothing_staged", "Stage at least one reply.")
    for reply in selected:
        blockers(session, state, reply, remote)
    payload = {"head": remote, "viewer": state["viewer"], "replies": copy.deepcopy(selected)}
    token = digest(payload)
    state["prepared"] = {"token": token, **payload}
    session.save(state)
    return {"owner": session.view(state), "token": token, "replies": selected}


def recover(session, state):
    for action in state["actions"]:
        if action["status"] not in ("submitting", "ambiguous"):
            continue
        resource = "issues" if action["kind"] == "timeline" else "pulls"
        values = gh(session, "comments", pages=True, resource=resource)
        if any(not isinstance(v, dict) or type(v.get("id")) is not int or not isinstance(v.get("body"), str)
               or not isinstance(v.get("user"), dict) or not isinstance(v["user"].get("login"), str) for v in values):
            raise ApiError("invalid_response", "Incomplete receipt lookup; refusing a duplicate reply.")
        matches = [v for v in values if valid_receipt(session, action, v)]
        if len(matches) > 1:
            raise ApiError("ambiguous_receipt", "Multiple matching receipts; inspect GitHub before retrying.")
        if matches:
            finish(state, action, matches[0])
        else:
            action["status"] = "unsent"
            for reply in state["replies"]:
                if reply["id"] == action["reply"] and reply["status"] != "published":
                    reply["status"] = "unstaged"
        session.save(state)
    state.pop("prepared", None)
    session.save(state)
    return session.view(state)


def finish(state, action, receipt):
    action.update(status="published", receipt=receipt)
    reply = next(r for r in state["replies"] if r["id"] == action["reply"])
    reply.update(status="published", receipt=receipt)


def publish(session, state, request):
    prepared = copy.deepcopy(state.get("prepared"))
    if not prepared or request.get("token") != prepared["token"]:
        raise ApiError("confirmation_required", "Prepare and inspect the exact staged replies before publishing.")
    # A second prepare rereads GitHub and verifies the exact confirmation, not merely the count.
    fresh = prepare(session, state)
    if fresh["token"] != prepared["token"]:
        state.pop("prepared", None)
        session.save(state)
        raise ApiError("stale_confirmation", "Publication changed; inspect a fresh preview.")
    for reply in prepared["replies"]:
        refresh(session, state)
        remote = workspace.fetch_head(session, state)
        if remote != prepared["head"]:
            raise ApiError("stale_confirmation", "PR head changed during publication; prepare the remaining replies again.")
        blockers(session, state, reply, remote)
        identity = uuid.uuid4().hex
        marker = f"<!-- pr-owner session={digest(str(session.path))[:24]} action={identity} -->"
        action = {"id": identity, "reply": reply["id"], "kind": reply["target"]["kind"],
                  "target": reply["target"], "viewer": state["viewer"], "marker": marker,
                  "payload": {"body": with_marker(reply["body"], marker)}, "status": "submitting"}
        state["actions"].append(action)
        state.pop("prepared", None)
        session.save(state)
        try:
            endpoint = f"comments/{reply['target']['root_id']}/replies" if action["kind"] == "reply" else "comments"
            receipt = gh(session, endpoint, action["payload"], resource="issues" if action["kind"] == "timeline" else "pulls")
            if not valid_receipt(session, action, receipt):
                raise ApiError("uncertain_publication", "Reply response could not be verified; recover before retrying.")
            finish(state, action, receipt)
        except BaseException:
            action["status"] = "ambiguous"
            session.save(state)
            raise
        session.save(state)
    return session.view(state)
