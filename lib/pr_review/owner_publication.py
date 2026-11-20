"""Owner reply drafts and commit provenance using forge-owned operations and receipts."""
import copy
import uuid

from .common import ApiError, digest, now, read_json, require_text
from .publication import publication_adapter
from .owner_state import feedback_body, refresh
from . import owner_git as workspace, owner_commit_plan, owner_contracts


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


def published_thread(session, state, item):
    """Resolve an imported GitHub inline comment by its original generation and publication receipt."""
    from .state import Session
    source = item.get('source', {})
    if session.repo.project.forge != 'github' or not all(source.get(k) for k in ('session', 'generation', 'comment')):
        return None
    try:
        review = Session({'session': source['session'], 'generation': source['generation']})
        if (review.manifest.get('repository'), review.manifest.get('pr'), review.manifest.get('git_common_dir')) != (
                session.repo.identity, session.repo.pr, str(session.repo.common)):
            return None
        ledger = read_json(review.file, {})
    except (ApiError, OSError):
        return None  # An unavailable Reviewer archive must not block unrelated Owner work.
    if ledger.get('generation') != source['generation']:
        return None
    comment = next((c for c in ledger.get('comments', []) if c['id'] == source['comment']), {})
    receipt = comment.get('receipt') or {}
    if comment.get('status') != 'published' or comment.get('placement', {}).get('kind') != 'inline' or not receipt.get('id'):
        return None
    for thread in state['feedback']:
        if thread['kind'] != 'thread' or thread.get('root_id') != receipt['id'] or len(thread['comments']) != 1:
            continue
        root = thread['comments'][0]
        if (root.get('url') == receipt.get('html_url') and root.get('url')
                and feedback_body(root['body']) == feedback_body(item['comments'][0]['body'])):
            return thread
    return None


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
        body = "Fixed in " + ", ".join(f"[{sha[:12]}]({session.repo.forge.commit_url(repository, sha)})" for sha in association["commits"]) + "."
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
    draft_verified_threads(session, state, batch, remote)
    batch["reply_issues"] = issues
    state.pop("prepared", None)
    return issues


def draft_verified_threads(session, state, batch, remote, *, latest=None):
    """Rebuild missing thread drafts from completed verification without modifying existing drafts."""
    repository = state.get('push_target', {}).get('repository')
    if not repository or batch.get('discussion_stale') or batch.get('integrity_failure'):
        return
    for outcome in (batch.get('review') or {}).get('outcomes', []):
        key = outcome['item']
        if latest is not None and latest.get(key) is not batch:
            continue
        item = next((i for i in state['feedback'] if i['key'] == key), None)
        if (not item or item.get('missing') or key not in batch.get('verified_items', [])
                or item['fingerprint'] != batch['verified_fingerprints'].get(key)):
            continue
        thread = published_thread(session, state, item) if item['kind'] == 'local_review' else item
        if (not thread or thread['kind'] != 'thread' or thread.get('missing') or thread.get('is_resolved')
                or not thread.get('can_reply') or any(r['source'] == thread['key'] for r in state['replies'])):
            continue
        extra = {}
        if outcome['status'] == 'already_addressed':
            baseline = batch.get('baseline')
            if (not baseline or baseline != batch.get('remote_head')
                    or session.repo.git('rev-parse', baseline + '^{tree}').stdout.strip() != batch.get('final_tree')
                    or (remote and not workspace.ancestor(session, baseline, remote))):
                continue
            commits = [baseline]
            prefix = 'Verified that this concern was already addressed at '
        elif outcome['status'] == 'fixed' and item['kind'] == 'local_review':
            commits = batch.get('commit_progress', {}).get('items', {}).get(key)
            if not commits or commits != batch.get('associations', {}).get(key, {}).get('commits'):
                continue
            pushed = bool(remote and all(workspace.ancestor(session, sha, remote) for sha in commits))
            extra = dict(automatic=True, verified_source=key, commit_repository=repository,
                         awaiting_push=not pushed, push_confirmed=pushed)
            prefix = 'Fixed in '
        else:
            continue
        links = ', '.join(f'[{sha[:12]}]({session.repo.forge.commit_url(repository, sha)})' for sha in commits)
        state['replies'].append(new_reply(thread, prefix + links + '.', batch=batch['id'], commits=commits,
                                          status='staged', **extra))
        state.pop('prepared', None)


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
    key = reply.get('verified_source', reply['source'])
    if key != reply['source']:
        local = next((i for i in state['feedback'] if i['key'] == key and not i.get('missing')), None)
        thread = published_thread(session, state, local) if local else None
        if (not batch or not thread or thread['key'] != reply['source']
                or local['fingerprint'] != batch.get('verified_fingerprints', {}).get(key)):
            return False
    if (not batch or batch["status"] != "ready" or not owner_contracts.current(batch) or batch.get("integrity_failure")
            or batch.get("commit_execution", {}).get("inspection_required")
            or key not in fixed_items(batch)
            or (key == reply["source"] and batch.get("verified_fingerprints", {}).get(key) != reply["fingerprint"])
            or reply.get("commit_repository") != state.get("push_target", {}).get("repository")):
        return False
    workspace.validate(session, state)
    head = session.repo.git("rev-parse", "HEAD", cwd=state["worktree"]).stdout.strip()
    progress = owner_commit_plan.inspect(session, batch, head)
    return progress["items"].get(key) == reply.get("commits")


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
    if batch:
        owner_contracts.require_current(batch)
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
    recover(session, state, statuses=('unsent',), clear_prepared=False, refresh_context=False)
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
    previous = state.get('prepared', {})
    operations = []
    for reply in selected:
        old = next((op for op in previous.get('operations', []) if op['reply'] == reply['id'] and op.get('body') == reply['body'] and op['target'] == reply['target'] and op['viewer'] == state['viewer']), None)
        if old is None:
            old = next((op for op in reversed(state['actions']) if op['status'] == 'unsent' and op['reply'] == reply['id'] and op.get('body') == reply['body'] and op['target'] == reply['target'] and op['viewer'] == state['viewer']), None)
        if old is not None:
            old = {**old, 'status': 'draft'}
        operations.append(copy.deepcopy(old) if old else publication_adapter(session).prepare_owner_reply(session, reply, state['viewer'], uuid.uuid4().hex))
    payload = {"operations": operations, "head": remote, "viewer": state["viewer"], "replies": copy.deepcopy(selected)}
    token = digest(payload)
    state["prepared"] = {"token": token, **payload}
    session.save(state)
    return {"owner": session.view(state), "token": token, "replies": selected}


def recover(session, state, *, statuses=('submitting', 'ambiguous'), clear_prepared=True, refresh_context=True):
    actions = [a for a in state['actions'] if a['status'] in statuses
               or (a['status'] == 'published' and a.get('attempt_count', 1) > 1)]
    if actions:
        from .forge import check_account
        _, account = session.repo.forge.owner_context(session.repo)
        check_account(state.get('forge_account'), account, state.get('viewer'))
        results = publication_adapter(session).lookup_owner(session, actions)
    for action in actions:
        matches = results[action['id']]
        if len(matches) > 1:
            reply = next(r for r in state['replies'] if r['id'] == action['reply'])
            reply['needs_attention'] = 'Multiple remote receipts match this reply; inspect the duplicate replies.'
            session.save(state)
            raise ApiError("ambiguous_receipt", "Multiple matching receipts; inspect GitHub before retrying.")
        if action['status'] == 'published' and not matches:
            # Remote deletion does not undo a completed send. Retain its journal
            # and keep auditing for late duplicates without blocking other replies.
            continue
        if action.get('receipt') and not matches:
            raise ApiError('ambiguous_receipt', 'A previously confirmed receipt is missing; inspect the remote reply before retrying.')
        if matches:
            action.setdefault('body', publication_adapter(session).owner_body(action))
            action['evidence'] = publication_adapter(session).owner_receipt(matches[0])
            reply = next(r for r in state['replies'] if r['id'] == action['reply'])
            if reply.get('receipt') and not reply.get('evidence'):
                reply['evidence'] = publication_adapter(session).owner_receipt(reply['receipt'])
            finish(state, action, matches[0])
        elif action["status"] != "unsent":
            action["status"] = "unsent"
            for reply in state["replies"]:
                if reply["id"] == action["reply"] and reply["status"] != "published":
                    reply["status"] = "unstaged"
        session.save(state)
    if refresh_context:
        refresh(session, state)
    if clear_prepared:
        state.pop("prepared", None)
    session.save(state)
    return session.view(state)


def finish(state, action, receipt):
    action.update(status="published", receipt=receipt)
    reply = next(r for r in state["replies"] if r["id"] == action["reply"])
    if reply['status'] == 'published':
        if (reply.get('evidence') or {}).get('id') != (action.get('evidence') or {}).get('id'):
            reply.setdefault('historical_receipts', []).append(receipt)
            if action.get('evidence'):
                reply.setdefault('historical_evidence', []).append(action['evidence'])
            reply['needs_attention'] = 'An additional earlier reply was recovered; inspect the duplicate remote replies.'
        return
    if action.get('body', reply['body']) != reply['body'] or action['target'] != reply['target']:
        reply.update(status='unstaged', needs_attention='An earlier reply was recovered; inspect its receipt before publishing edited wording.')
        reply.setdefault('historical_receipts', []).append(receipt)
        if action.get('evidence'):
            reply.setdefault('historical_evidence', []).append(action['evidence'])
    else:
        reply.update(status="published", receipt=receipt, evidence=action.get('evidence'))


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
        action = copy.deepcopy(next(op for op in prepared['operations'] if op['reply'] == reply['id']))
        action['status'] = 'submitting'
        previous = next((a for a in state['actions'] if a['id'] == action['id']), None)
        if previous is not None:
            action['attempt_count'] = previous.get('attempt_count', 1) + 1
            previous.update(action)
            action = previous
        else:
            action["attempt_count"] = 1
            state["actions"].append(action)
        state.pop("prepared", None)
        session.save(state)
        try:
            receipt = publication_adapter(session).submit(session, action)
            if not publication_adapter(session).matches(session, action, receipt):
                raise ApiError('uncertain_publication', 'Reply response could not be verified; recover before retrying.')
            action['receipt'] = receipt
            action['evidence'] = publication_adapter(session).owner_receipt(receipt)
            session.save(state)
            finish(state, action, receipt)
        except BaseException:
            action["status"] = "ambiguous"
            session.save(state)
            raise
        session.save(state)
    return session.view(state)


def valid_receipt(session, action, value):
    """Compatibility for historical Owner callers."""
    return publication_adapter(session).matches(session, action, value)
