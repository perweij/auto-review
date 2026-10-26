"""Explicit local handoff of saved reviewer comments to the owner fix queue."""
import copy

from .common import ApiError, digest, locked, now, read_json, require_text
from .repository import Repository
from .state import Session
from .owner_state import OwnerSession, open_session
from .owner_completion import manual_triage


def from_review(request):
    source = Session(request)
    repo = Repository.from_manifest(source.manifest)
    opened = open_session({"cwd": str(repo.cwd), "repository": repo.identity, "pr": repo.pr,
                           "defaults": request.get("defaults")})
    owner = OwnerSession({"session": opened["session"]})
    # Follow execution -> PR -> state order. Never modify the review's drafts.
    with locked(owner.execution_lock):
        with locked(source.pr_lock), locked(source.lock):
            source = Session(request)
            if (source.manifest["repository"], source.manifest["pr"], source.manifest["git_common_dir"]) != (
                    owner.repo.identity, owner.repo.pr, str(owner.repo.common)):
                raise ApiError("invalid_session", "Reviewer identity changed; reopen it before sending findings.")
            review = source.load()
            source.check_write(request, review)
            keys = request.get("comments")
            if (not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys)
                    or len(set(keys)) != len(keys)):
                raise ApiError("invalid_request", "Select distinct saved reviewer comments.")
            comments = {c["id"]: c for c in review["comments"]}
            if any(k not in comments for k in keys):
                raise ApiError("invalid_target", "A selected reviewer comment is no longer available.")
            findings = {f["id"]: f for f in read_json(source.path / "reviews/synthesis.json", {}).get("findings", [])}
            imported = []
            for key in keys:
                comment = comments[key]
                body = require_text(comment.get("body"), "saved reviewer comment")
                identity = "local:" + digest([str(source.root), source.id, key])[:32]
                context = source.comment_context(comment, review)
                source_data = {"session": str(source.root), "generation": source.id, "comment": key,
                               "revision": request["revision"], "snapshot": copy.deepcopy(source.manifest["snapshot"]),
                               "finding_ids": copy.deepcopy(comment.get("finding_ids", [])),
                               "findings": [copy.deepcopy(findings[k]) for k in comment.get("finding_ids", []) if k in findings]}
                item = {"key": identity, "kind": "local_review", "title": comment.get("title", "Local review finding"),
                        "source": source_data, "review_context": copy.deepcopy(context),
                        "placement": copy.deepcopy(comment.get("placement", {})),
                        "evidence": copy.deepcopy(comment.get("evidence", [])),
                        "locations": copy.deepcopy(comment.get("locations", [])),
                        "path": context.get("path"), "line": context.get("line"),
                        "comments": [{"id": key, "author": "Saved reviewer finding", "body": body}],
                        "actionable": True, "unread": False, "queued": True, "triage": "attention", "attention": True}
                # Unrelated edits/staging change the source revision, not this finding.
                item["fingerprint"] = digest({**item, "source": {k: v for k, v in source_data.items() if k != "revision"}})
                imported.append(item)
            source.git.pin(source.id)
        state = owner.load()
        previous = {i["key"]: i for i in state["feedback"]}
        changed = set()
        for index, item in enumerate(imported):
            old = previous.get(item["key"])
            if old and old["fingerprint"] == item["fingerprint"]:
                imported[index] = {**old, "queued": True, "triage": "attention", "attention": True}
                # Re-import is an explicit request to reconsider this finding.
                manual_triage(state, imported[index])
            else:
                item.update(imported_at=now(), versions=copy.deepcopy(old.get("versions", [])) if old else [])
                if old:
                    item["versions"].append({k: copy.deepcopy(v) for k, v in old.items() if k != "versions"})
                    changed.add(item["key"])
        selected = [i["key"] for i in imported]
        # Keep the handoff contiguous and in the user's selection order.
        state["feedback"] = imported + [i for i in state["feedback"] if i["key"] not in selected]
        for batch in state["batches"]:
            if changed.intersection(batch["items"]):
                batch["discussion_stale"] = True
        state.pop("prepared", None)
        owner.save(state)
        return {"owner": owner.view(state), "items": selected}
