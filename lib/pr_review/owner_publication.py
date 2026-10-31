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


def bind_exact(batch, progress):
    """Keep explicit human associations when recording exact local matches."""
    associations = batch.setdefault("associations", {})
    for key, commits in progress["items"].items():
        if key in fixed_items(batch) and (key not in associations or associations[key]["method"] in ("exact", "task")):
            associations[key] = {"commits": commits, "method": "exact" if progress["method"] == "exact" else "task", "at": now()}


def draft_fixed(session, state, batch, remote=None, automatic=False):
    """Prepare destination-specific drafts; automatic drafts may await a push."""
    issues = []
    pending = {a["reply"] for a in state["actions"] if a["status"] in ("ambiguous", "submitting")}
    for key, association in batch.get("associations", {}).items():
        association["refs"] = [workspace.pin_commit(session, sha) for sha in association["commits"]]
        item = next((item for item in state['feedback'] if item['key'] == key), None)
        if not item or item.get('missing'):
            issues.append({'item': key, 'reason': 'Feedback is no longer available; its commit association is retained as history.'})
            continue
        if item["kind"] in ("local_review", "issue_plan"):
            continue
        if automatic and batch.get("commit_progress", {}).get("items", {}).get(key) != association["commits"]:
            issues.append({"item": key, "reason": "An explicit association differs from this fix; inspect its reply."})
            continue
        if not automatic and not all(workspace.ancestor(session, sha, remote) for sha in association["commits"]):
            issues.append({"item": key, "kind": "awaiting_push",
                           "reason": "Commit linked locally; push it, then associate again to create the reply."})
            continue
        repository = state["push_target"]["repository"]
        reason = None
        if not repository:
            reason = "GitHub no longer identifies the PR head repository; inspect the destination."
        elif item["fingerprint"] != batch["verified_fingerprints"].get(key) or item.get("missing"):
            reason = "Feedback changed; inspect the reply context."
        elif item["kind"] == "thread" and not item.get("can_reply"):
            reason = "GitHub does not permit replying to this feedback."
        existing = next((r for r in state["replies"] if r.get("batch") == batch["id"] and r["source"] == key), None)
        if automatic and not existing and any(r["source"] == key and r["status"] != "published" for r in state["replies"]):
            reason = "An existing reply needs inspection; no duplicate was created."
        if reason:
            issues.append({"item": key, "reason": reason})
            if not automatic and not repository:
                raise ApiError("missing_head_repository", reason)
            continue
        body = "Fixed in " + ", ".join(f"[{sha[:12]}](https://{repository}/commit/{sha})" for sha in association["commits"]) + "."
        if item["kind"] != "thread":
            body = f"In response to [this feedback]({item['comments'][0]['url']}):\n\n" + body
        if existing:
            if existing["id"] in pending:
                if automatic:
                    issues.append({"item": key, "reason": "Recover the uncertain publication before changing this reply."})
                continue
            if not automatic and association["method"] == "explicit" and existing["status"] != "published":
                # An inspected manual reassociation can replace exact local proof
                # (e.g. after rebasing). It still needs the normal pushed-commit checks.
                for field in ("automatic", "awaiting_push", "push_confirmed", "commit_repository"):
                    existing.pop(field, None)
            if existing["status"] != "published" and existing.get("commits") != association["commits"]:
                existing.update(commits=association["commits"], status="unstaged", needs_attention="Commit association changed; inspect this reply.")
                if not existing.get("human_edited"):
                    existing["body"] = body
            if automatic and existing.get("needs_attention"):
                issues.append({"item": key, "reason": existing["needs_attention"]})
            continue  # Preserve wording, publication, and explicit unstaging.
        extra = {"automatic": True, "awaiting_push": True, "push_confirmed": False,
                 "commit_repository": repository} if automatic else {}
        state["replies"].append(new_reply(item, body, batch=batch["id"], commits=association["commits"], status="staged", **extra))
    batch["reply_issues"] = issues
    state.pop("prepared", None)
    return issues


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
            if not batch.get("commit_execution", {}).get("inspection_required"):
                bind_exact(batch, progress)
        draft_fixed(session, state, batch, remote)
    invalidate(session, state, remote)
    state.pop("prepared", None)
    session.save(state)
    return session.view(state)


def new_reply(item, body, **extra):
    if item["kind"] in ("local_review", "issue_plan"):
        raise ApiError("local_only", "This finding is local; choose a GitHub conversation to compose a reply.")
    return {"id": uuid.uuid4().hex, "source": item["key"], "fingerprint": item["fingerprint"],
            "target": {"kind": "reply", "root_id": item["root_id"]} if item["kind"] == "thread" else {"kind": "timeline"},
            "body": body, "status": "unstaged", "versions": [], "created_at": now(), **extra}


def automatic_proof(session, state, reply):
    """A staged-before-push claim must still have its exact local proof."""
    batch = next((b for b in state["batches"] if b["id"] == reply.get("batch")), None)
    if (not batch or batch["status"] != "ready" or batch.get("integrity_failure")
            or batch.get("commit_execution", {}).get("inspection_required")
            or reply["source"] not in fixed_items(batch)
            or batch.get("verified_fingerprints", {}).get(reply["source"]) != reply["fingerprint"]
            or reply.get("commit_repository") != state.get("push_target", {}).get("repository")):
        return False
    workspace.validate(session, state)
    head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
    progress = owner_commit_plan.inspect(session, batch, head)
    return progress["items"].get(reply["source"]) == reply.get("commits")


def invalidate(session, state, remote):
    items = {i["key"]: i for i in state["feedback"]}
    for reply in state["replies"]:
        if reply["status"] == "published":
            continue
        item = items.get(reply["source"])
        reason = None
        if not item or item.get("missing") or item["fingerprint"] != reply["fingerprint"]:
            reason = "Discussion changed; inspect and acknowledge this reply."
        elif reply.get("automatic") and not automatic_proof(session, state, reply):
            reason = "The exact local fix or reply destination changed; inspect and reassociate this reply."
        elif reply.get("commits") and any(not workspace.ancestor(session, sha, remote) for sha in reply["commits"]):
            if reply.get("automatic") and not reply.get("push_confirmed"):
                reply["awaiting_push"] = True
            else:
                reason = "Associated commit is not on the current PR; push or reassociate before staging."
        elif reply.get("automatic"):
            reply.update(awaiting_push=False, push_confirmed=True)
        if reason:
            reply.update(status="unstaged", needs_attention=reason)


def blockers(session, state, reply, remote, allow_unpushed=False):
    item = session.items(state, [reply["source"]])[0]
    if item["kind"] in ("local_review", "issue_plan"):
        raise ApiError("local_only", "Local findings do not have a GitHub reply destination.")
    if reply.get("needs_attention") or item["fingerprint"] != reply["fingerprint"]:
        raise ApiError("stale_discussion", "Inspect and acknowledge the changed reply context first.")
    if reply.get("automatic") and not automatic_proof(session, state, reply):
        raise ApiError("stale_commit", "The exact local fix or reply destination changed; inspect this reply.")
    batch = next((b for b in state["batches"] if b["id"] == reply.get("batch")), None)
    if batch and any(u.get("kind") == "integration_repair"
                     for u in owner_commit_plan.build(session, batch)["units"]):
        proof_head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip() if allow_unpushed else remote
        if not owner_commit_plan.inspect(session, batch, proof_head)["complete"]:
            raise ApiError("unpublished_commit", "The verified integration repair is required before publishing replies. Push the complete series first.")
    require_text(reply["body"], "reply body")
    if item["kind"] == "thread" and not item.get("can_reply"):
        raise ApiError("permission_denied", "GitHub does not permit replying to this thread.")
    if reply.get("commits") and any(not workspace.ancestor(session, sha, remote) for sha in reply["commits"]):
        if not (allow_unpushed and reply.get("automatic") and not reply.get("push_confirmed")):
            raise ApiError("unpublished_commit", "The fix commits are not on the current PR head. Push first, then retry P; no replies were omitted.")


def prepare(session, state):
    selected_ids = {r["id"] for r in state["replies"] if r["status"] == "staged"}
    refresh(session, state)
    remote = workspace.fetch_head(session, state)
    invalidate(session, state, remote)
    session.save(state)
    if state["metadata"]["state"] != "OPEN":
        raise ApiError("closed_pr", "Publishing owner replies requires an open PR.")
    if any(a["status"] in ("submitting", "ambiguous") for a in state["actions"]):
        raise ApiError("uncertain_publication", "Recover uncertain replies before publishing again.")
    selected = [r for r in state["replies"] if r["id"] in selected_ids]
    if not selected:
        raise ApiError("nothing_staged", "Stage at least one reply.")
    for reply in selected:
        blockers(session, state, reply, remote)
        if reply["status"] != "staged":
            raise ApiError("stale_reply", "A selected reply changed; inspect and stage it again. Nothing was omitted from the preview.")
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
        if state['metadata']['state'] != 'OPEN':
            state.pop('prepared', None)
            session.save(state)
            raise ApiError('closed_pr', 'The PR closed during publication; confirmed replies are retained and remaining replies were not sent.')
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
