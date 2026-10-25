"""Validated, limited-scope analysis artifacts; never a fabricated full review."""
from .common import ApiError, locked, now, read_json
from .workflow import thread_fingerprint, affected_drafts


def prepare(session):
    keys = session.manifest.get("followup_threads", [])
    threads = {t["key"]: t for t in session.discussion().get("items", []) if t.get("kind") == "thread"}
    if not keys or any(key not in threads for key in keys):
        raise ApiError("invalid_target", "A selected conversation is missing; refresh GitHub and select existing threads.")
    return {"head": session.git.head, "merge_base": session.git.base,
            "threads": [threads[k] for k in keys],
            "fingerprints": {k: thread_fingerprint(threads[k]) for k in keys}}


def validate(session, result):
    keys = session.manifest.get("followup_threads", [])
    if not isinstance(result, dict) or set(result) != {"assessments"} or not isinstance(result["assessments"], list):
        raise ApiError("invalid_assessment", "Follow-up returned no assessment list.")
    fields = {"thread_key", "outcome", "evidence", "limitations", "suggested_reply"}
    values = result["assessments"]
    for item in values:
        if (not isinstance(item, dict) or set(item) != fields or
                any(not isinstance(item[k], str) for k in fields) or not item["evidence"].strip() or
                item["outcome"] not in {"ADDRESSED", "STILL_PRESENT", "NOT_APPLICABLE", "UNCERTAIN"}):
            raise ApiError("invalid_assessment", "Follow-up must provide an outcome and evidence for each conversation.")
    if sorted(i["thread_key"] for i in values) != sorted(keys):
        raise ApiError("invalid_assessment", "Follow-up must assess every selected conversation exactly once.")
    return result


def adopt(session):
    # Cached follow-ups may still be current and editable. Take the same short
    # transaction lock as the frontend before loading and updating its ledger.
    with locked(session.lock, wait=True):
        state = session.load()
        discussion = session.discussion()
        affected = affected_drafts(state["discussion"], discussion, state["comments"])
        for comment in state["comments"]:
            if comment["id"] in affected:
                comment.update(status="unstaged", needs_attention="Discussion changed; inspect before staging again.")
        state["discussion"] = discussion
        state["discussion_hash"] = session.manifest.get("discussion", {}).get("hash")
        pr = read_json(session.path / "publication/pr-latest.json", {})
        state["checked"] = {"at": now(), "head": pr.get("headRefOid"), "pr": pr}
        state.pop("prepared", None)
        session.save(state)
