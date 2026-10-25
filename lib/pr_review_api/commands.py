"""Explicit local draft mutations; no model or network work here."""
import copy
import uuid

from .common import ApiError, digest, now, require_text
from .state import Session


def comment_by_id(state, identity):
    comment = next((c for c in state["comments"] if c["id"] == identity), None)
    if comment is None:
        raise ApiError("missing_comment", "The selected comment no longer exists.")
    return comment


def remember(comment):
    comment.setdefault("versions", []).append({"at": now(), **{k: copy.deepcopy(comment.get(k)) for k in
        ("body", "placement", "override", "status", "provenance")}})


def mutable(comment, state):
    if comment["status"] == "published":
        raise ApiError("published", "Published comments are read-only; create a follow-up.")
    if any(a["status"] in ("submitting", "ambiguous") and comment["id"] in a["comment_ids"]
           for b in state["batches"] for a in b["actions"]):
        raise ApiError("ambiguous_publication", "Resolve this comment's previous submission before editing it.")


def new_comment(body, title="Overall assessment", placement=None, provenance=None):
    return {"id": "u" + uuid.uuid4().hex[:16], "title": title, "body": body,
            "placement": placement or {"kind": "unplaced"}, "automatic": {"kind": "unplaced"},
            "override": True, "status": "unstaged", "classification": "HUMAN",
            "finding_ids": [], "evidence": [], "locations": [], "explanation": "",
            "partial_coverage": False, "human_edited": True, "versions": [], "receipt": None,
            "provenance": provenance}


def placement_for(session, state, value):
    if not isinstance(value, dict):
        raise ApiError("invalid_request", "Placement must be an object.")
    if value.get("kind") == "inline":
        return session.git.anchor(value)
    if value.get("kind") == "reply":
        thread = next((t for t in state["discussion"].get("items", [])
                       if t.get("kind") == "thread" and t.get("key") == value.get("key")), None)
        if not thread or not thread.get("can_reply"):
            raise ApiError("invalid_target", "Choose an existing thread that accepts replies.")
        return {"kind": "reply", "key": thread["key"], "root_id": thread["root_id"]}
    if value.get("kind") == "review":
        return {"kind": "review"}
    raise ApiError("invalid_target", "Choose code, an existing thread, or an overall assessment.")


def preview_comment(session, state, request):
    """Render a new comment in context without saving it or changing revision."""
    revision = digest(state)
    state = copy.deepcopy(state)
    placement = placement_for(session, state, request.get("placement"))
    comment = new_comment(require_text(request.get("body", ""), "body", empty=True),
                          require_text(request.get("title", "New comment"), "title"), placement)
    state["comments"].append(comment)
    review = session.view(state)
    review["revision"] = revision
    preview = next(c for c in review["comments"] if c["id"] == comment["id"])
    preview["composing"] = True
    preview["blockers"] = [b for b in preview["blockers"] if b != "Comment text is empty."]
    preview["bulk_blockers"] = [b for b in preview["bulk_blockers"] if b != "Comment text is empty."]
    return {"review": review, "comment": comment["id"]}


def mutate(session, state, request):
    operation = request.get("operation")
    changed, skipped = [], []
    if operation == "verdict":
        verdict = request.get("verdict")
        if verdict not in ("COMMENT", "REQUEST_CHANGES", "APPROVE"):
            raise ApiError("invalid_request", "Unknown verdict.")
        reason = next(v for v in session.verdicts(state) if v["value"] == verdict)
        if not reason["available"]:
            raise ApiError("verdict_unavailable", reason["reason"])
        if state["verdict_submitted"] and any(a["status"] not in ("published", "superseded") for b in state["batches"] for a in b["actions"]):
            raise ApiError("partial_publication", "Finish or explicitly abandon the remaining batch before starting another verdict.")
        state["verdict"] = verdict
        state["verdict_submitted"] = False
        state["verdict_requested"] = True
    elif operation == "create":
        body = require_text(request.get("body", ""), "body", empty=True)
        placement = placement_for(session, state, request["placement"]) if request.get("placement") else {"kind": "unplaced"}
        comment = new_comment(body, require_text(request.get("title", "New comment"), "title"), placement)
        state["comments"].append(comment)
        changed.append(comment["id"])
    elif operation == "recover":
        source = Session({"session": str(session.root), "generation": request.get("source_generation")})
        source_state = source.load()
        if request.get("document") is not None:
            document = next((d for d in source_state["documents"] if d["name"] == request["document"]), None)
            if not document:
                raise ApiError("missing_document", "Saved document not found.")
            body, title = document["text"], "Recovered historical text"
        else:
            original = comment_by_id(source_state, request.get("source_comment"))
            version = request.get("version")
            if version is not None:
                if type(version) is not int or not 0 <= version < len(original["versions"]):
                    raise ApiError("invalid_request", "Unknown saved text version.")
                body = original["versions"][version]["body"]
            else:
                body = original["body"]
            title = original["title"]
        provenance = {"generation": source.id, "comment": request.get("source_comment"),
                      "version": request.get("version"), "document": request.get("document"), "at": now()}
        if request.get("comment"):
            comment = comment_by_id(state, request["comment"])
            mutable(comment, state)
            remember(comment)
            comment.update(body=body, status="unstaged", human_edited=True, provenance=provenance)
        else:
            comment = new_comment(body, title, provenance=provenance)
            state["comments"].append(comment)
        changed.append(comment["id"])
    elif operation in ("stage", "unstage", "suppress", "restore", "edit", "placement", "automatic", "acknowledge"):
        ids = request.get("comments", [request.get("comment")])
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            raise ApiError("invalid_request", "Specify distinct comment IDs.")
        for identity in ids:
            comment = comment_by_id(state, identity)
            mutable(comment, state)
            if operation == "stage":
                reasons = session.blockers(comment, state, bulk=request.get("bulk", False))
                if comment["status"] == "suppressed":
                    reasons.append("Restore this suppressed comment before staging.")
                if reasons:
                    skipped.append({"id": identity, "reasons": reasons})
                    continue
                comment["status"] = "staged"
            elif operation in ("unstage", "suppress", "restore"):
                comment["status"] = "suppressed" if operation == "suppress" else "unstaged"
            elif operation == "edit":
                body = require_text(request.get("body"), "body", empty=True)
                if comment["body"] != body:
                    remember(comment)
                    comment.update(body=body, status="unstaged", human_edited=True)
            elif operation == "acknowledge":
                # Explicit individual inspection acknowledges fresh discussion; it does not stage.
                comment.pop("needs_attention", None)
            else:
                place = copy.deepcopy(comment["automatic"]) if operation == "automatic" else placement_for(session, state, request.get("placement"))
                if comment["placement"] != place or comment["override"] != (operation != "automatic"):
                    remember(comment)
                    comment.update(placement=place, override=operation != "automatic", status="unstaged")
                    comment.pop("needs_attention", None)
            changed.append(identity)
    else:
        raise ApiError("invalid_request", "Unknown draft operation.")
    # A local change invalidates an earlier publication confirmation, without
    # discarding submitted receipts or ambiguous recovery information.
    state.pop("prepared", None)
    session.save(state)
    return {"review": session.view(state), "changed": changed, "skipped": skipped}
