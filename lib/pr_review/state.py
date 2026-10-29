"""Saved generations and explicit human draft state. Reads never invoke a model."""
import copy
from pathlib import Path

from .common import ApiError, artifact_path, atomic_json, digest, git, now, read_json, state_root
from .context import GitContext, parse_hunks
from .assessments import comment_assessment, finding_assessments
from . import workflow
from .receipts import import_legacy


def has_manual_versions(comment):
    """Only explicitly automatic snapshots are exempt from manual/unknown history."""
    return any(not isinstance(v, dict) or v.get('actor') != 'automatic' for v in comment.get('versions', []))


def generation(path, manifest):
    snapshot = manifest.get("snapshot", {})
    # Base can advance without changing the reviewed diff. That metadata refresh
    # must never turn the same directory into a new generation and hide edits.
    return digest([str(path), snapshot.get("head"), snapshot.get("merge_base"), manifest.get("run_id")])[:24]


def discover(request):
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                 cwd=request.get("cwd", ".")).stdout.strip()
    sessions = []
    for file in state_root().glob("*/*/*/*/manifest.json"):
        try:
            m = read_json(file, {})
        except ApiError:
            continue  # An unrelated damaged run must not hide usable reviews.
        if not isinstance(m, dict):
            continue
        if m.get("git_common_dir") != common or (request.get("pr") and str(m.get("pr")) != str(request["pr"])):
            continue
        if request.get("repository") and m.get("repository") != request["repository"]:
            continue
        if m.get("session_directory") and Path(m["session_directory"]).resolve() != file.parent.resolve():
            continue
        session = Session({"session": str(file.parent)})
        sessions.append({"session": str(session.root), "repository": m["repository"], "pr": m["pr"],
                         "url": m.get("url"), "generation": session.id,
                         "head": session.manifest.get("snapshot", {}).get("head"), "created_at": m.get("created_at"),
                         "automation": session.automation_summary()})
    return {"sessions": sorted(sessions, key=lambda x: x.get("created_at") or "", reverse=True)}


class Session:
    def __init__(self, request):
        self.root = artifact_path(request.get("session"))
        root_manifest = read_json(self.root / "manifest.json")
        if not isinstance(root_manifest, dict) or not root_manifest.get("git_common_dir"):
            raise ApiError("invalid_session", "Saved session manifest is missing.")
        if root_manifest.get("session_directory"):
            self.root = artifact_path(root_manifest["session_directory"])
        self.current = self.pointer(".current-review") or self.root
        self.path = self.current
        if request.get("generation"):
            candidates = [p for p in self.paths() if generation(p, read_json(p / "manifest.json", {})) == request["generation"]]
            if not candidates:
                raise ApiError("missing_generation", "This saved generation is unavailable; refresh history.")
            self.path = candidates[0]
        self.manifest = read_json(self.path / "manifest.json", {})
        self.id = generation(self.path, self.manifest)
        self.git = GitContext(self.manifest)
        self.file = self.path / "publication/drafts.json"
        self.lock = self.path / "publication/.drafts.lock"
        self.pr_lock = Path(self.manifest["git_common_dir"]) / "pr-review-locks" / f'{self.manifest["pr"]}.lock'

    def pointer(self, name):
        p = self.root / name
        return artifact_path(str(p)) if p.is_symlink() else None

    def paths(self):
        paths = {self.root, self.current}
        pending = self.pointer(".pending-review")
        if pending:
            paths.add(pending)
        # Attempts can contain nested snapshot archives; all stay beside this session.
        history = Path(str(self.root) + "-history")
        if history.exists():
            paths.update(p.parent.resolve() for p in history.rglob("manifest.json"))
        for field in ("snapshot_history", "review_history"):
            for entry in read_json(self.root / "manifest.json", {}).get(field, []):
                if entry.get("archive"):
                    p = artifact_path(entry["archive"])
                    if (p / "manifest.json").is_file():
                        paths.add(p)
        return sorted(paths, key=str)

    def discussion(self):
        p = self.manifest.get("discussion", {}).get("path")
        # Copies may contain absolute paths from the source attempt or another host.
        local = self.path / "discussion" / Path(p or "").parent.name / "discussion.json"
        if local.is_file():
            p = local
        elif not p or not Path(p).is_relative_to(self.path):
            return {"items": []}
        value = read_json(p, {"items": []})
        raw = read_json(Path(p).parent / "inline.json", [])
        by_id = {c.get("id"): c for c in raw}
        for item in value.get("items", []):
            if item.get("kind") == "thread":
                item["anchor"] = by_id.get(item.get("root_id"), {})
        return value

    def load(self):
        state = read_json(self.file)
        if state and state.get("generation") == self.id:
            if state.get("format_version") != 1:
                raise ApiError("unsupported_state", "This draft format requires a newer backend.")
            if not state.get("verdict_requested") and not state.get("verdict_submitted"):
                state["verdict"] = "COMMENT"
            state["documents"] = self.documents(read_json(self.path / "publication/contributions.json", {}))
            return state
        followup = self.manifest.get("analysis_kind") == "followup"
        ledger = {} if followup else read_json(self.path / "publication/contributions.json", {})
        synthesis = {} if followup else read_json(self.path / "reviews/synthesis.json", {})
        findings = {f["id"]: f for f in synthesis.get("findings", [])}
        # A copied API ledger is a different generation. Never auto-carry its human work.
        fresh = bool(state)
        comments = []
        actions = read_json(self.path / "publication/actions.json", [])
        covered = {f for a in actions if a.get("status") == "published"
                   and (not fresh or f"generation={self.id} " in a.get("marker", ""))
                   for f in a.get("finding_ids", [])}
        for proposal in ledger.get("proposals", []):
            basis = proposal.get("basis", {})
            ids = basis.get("finding_ids", [])
            fs = [findings[f] for f in ids if f in findings]
            primary = next((f.get("primary_location") for f in fs if f.get("primary_location")), None)
            destination = proposal.get("destination", {})
            if destination.get("kind") == "reply":
                placement = {"kind": "reply", "key": destination.get("key"), "root_id": destination.get("root_id")}
            else:
                placement = {"kind": "inline", **primary} if primary else {"kind": "unplaced"}
            classification = proposal.get("classification", basis.get("classification", "UNCERTAIN"))
            all_posted = bool(ids) and all(i in covered for i in ids)
            status = "published" if all_posted else "suppressed" if classification == "DUPLICATE" else "unstaged"
            finding_body = "\n\n".join(f.get("body_md", "") for f in fs)
            body = (basis if fresh else proposal).get("body_md") or finding_body
            # Empty generated bodies (notably duplicates) use the same display fallback.
            generated_body = basis.get("body_md", body) or finding_body
            comments.append({"id": proposal["id"], "title": "; ".join(f["title"] for f in fs) or "Review comment",
                             "body": body, "placement": placement, "automatic": copy.deepcopy(placement),
                             "override": False, "status": status, "classification": classification,
                             "finding_ids": ids, "evidence": [f.get("evidence", "") for f in fs],
                             "locations": [loc for f in fs for loc in f.get("locations", [])],
                             "explanation": basis.get("explanation", ""),
                             "discussion_keys": basis.get("discussion_keys", []),
                             "partial_coverage": any(i in covered for i in ids) and not all_posted,
                             "human_edited": body != generated_body, "versions": [], "receipt": None})
        recommendation = synthesis.get("recommendation") if self.manifest.get("analysis_kind") == "followup" else synthesis.get("recommendation", "COMMENT")
        return {"format_version": 1, "generation": self.id, "sequence": 0,
                "mutation_tracking": bool(self.manifest.get('draft_mutations_tracked')),
                "created_at": now_from_manifest(self.manifest), "comments": comments,
                "verdict": "COMMENT",
                "verdict_submitted": False, "verdict_requested": False, "batches": [] if fresh else import_legacy(self.path, self.git.head),
                "discussion": self.discussion(), "discussion_hash": self.manifest.get("discussion", {}).get("hash"),
                "checked": None, "recommendation": recommendation,
                "blocking_findings": synthesis.get("blocking_findings", 0), "documents": self.documents(ledger)}

    def documents(self, ledger):
        files = sorted((self.path / "publication").glob("review-body-history-*.md"))
        main = self.path / "publication/review-body.md"
        if main.is_file():
            import hashlib
            if hashlib.sha256(main.read_bytes()).hexdigest() != ledger.get("main_generated_hash"):
                files.append(main)
        return [{"name": p.name, "text": p.read_bytes().decode("utf-8"), "kind": "legacy_combined_body"} for p in files]

    def check_write(self, request, state):
        if self.path != (self.pointer(".current-review") or self.root):
            raise ApiError("historical", "A newer review is current. Keep your text and recover it into the current review.")
        if request.get("generation") != self.id or request.get("revision") != digest(state):
            raise ApiError("stale_revision", "The saved draft changed. Your unsaved text has not been applied; refresh or recover it.",
                           generation=self.id, revision=digest(state))
        ready = ((self.path / "reviews/followup.json").is_file()
                 and self.manifest.get("phases", {}).get("followup", {}).get("status") == "completed"
                 if self.manifest.get("analysis_kind") == "followup"
                 else (self.path / "publication/contributions.json").is_file())
        if not self.manifest.get("snapshot") or not ready:
            raise ApiError("not_ready", "This review has not produced drafts yet.")

    def save(self, state):
        self.git.pin(self.id)
        state["sequence"] += 1
        state["updated_at"] = now()
        atomic_json(self.file, state)

    def blockers(self, comment, state, *, bulk=False):
        from .suggestions import blockers
        reasons = blockers(self, comment, state, bulk=bulk)
        if comment.get("status") == "published":
            return ["Already published; create a follow-up instead."]
        if not comment.get("body", "").strip():
            reasons.append("Comment text is empty.")
        if comment.get("partial_coverage"):
            reasons.append("Some findings were already posted; split or rewrite this comment and create a new draft.")
        placement = comment.get("placement", {})
        if placement.get("kind") == "inline":
            try:
                self.git.anchor(placement)
            except ApiError as exc:
                reasons.append(str(exc))
        elif placement.get("kind") == "reply":
            thread = next((t for t in state["discussion"].get("items", []) if t.get("key") == placement.get("key")
                           and t.get("root_id") == placement.get("root_id")), None)
            if not thread or not thread.get("can_reply"):
                reasons.append("The selected thread is missing or no longer accepts replies.")
            elif bulk and (thread.get("is_resolved") or thread.get("is_outdated")):
                reasons.append("Stage this resolved/outdated thread reply individually after inspecting it.")
        elif placement.get("kind") != "review":
            reasons.append("Choose a code location, thread, or explicit overall assessment.")
        if bulk and comment.get("classification") in ("REVISIT", "UNCERTAIN", "DUPLICATE"):
            reasons.append({"REVISIT": "This finding revisits an existing discussion; inspect and stage individually or exclude it.",
                            "UNCERTAIN": "This finding is uncertain; inspect and stage individually or exclude it.",
                            "DUPLICATE": "This finding duplicates existing discussion; inspect it before restoring and staging individually."}[comment['classification']])
        if comment.get("needs_attention"):
            reasons.append(comment["needs_attention"])
        return reasons

    def view(self, state):
        from .operations import process_start
        comments = copy.deepcopy(state["comments"])
        assessments = finding_assessments({} if self.manifest.get("analysis_kind") == "followup"
                                         else read_json(self.path / "reviews/synthesis.json", {}))
        for c in comments:
            from .suggestions import identity, preview, render
            c['suggestion_preview'] = preview(c)
            if c['suggestion_preview']:
                batch = state.get('suggestion_batch', {})
                c['suggestion_preview']['batch_verification'] = (
                    batch if not batch or identity(self, c) in batch.get('members', []) else
                    dict(status='not_verified', evidence='This suggestion version was not included in the saved combined verification.'))
            c['publication_body'] = render(c)
            # Display metadata does not affect the draft revision, staging,
            # prepared publication, or any saved human wording.
            c.update(comment_assessment(c, assessments))
            c["blockers"] = self.blockers(c, state) if c["status"] != "published" else []
            c["bulk_blockers"] = self.blockers(c, state, bulk=True) if c["status"] != "published" else []
            c["context"] = self.comment_context(c, state)
        current = self.pointer(".current-review") or self.root
        pending = self.pointer(".pending-review")
        active = []
        for file in (state_root() / "operations").glob("*.json"):
            op = read_json(file, {})
            if (op.get("status") == "running" and op.get("common") == self.manifest["git_common_dir"]
                    and op.get("pr") == str(self.manifest["pr"]) and op.get("pid")
                    and process_start(op["pid"]) == op.get("start")):
                active.append({"operation_id": file.stem, "kind": op["kind"]})
        thread_contexts = {t["key"]: self.comment_context({"placement": {"kind": "reply", "key": t["key"]}}, state)
                           for t in state["discussion"].get("items", []) if t.get("kind") == "thread"}
        threads_by_key = {t["key"]: t for t in state["discussion"].get("items", []) if t.get("kind") == "thread"}
        for key, context in thread_contexts.items():
            thread = threads_by_key[key]
            context.setdefault("thread", thread)
            context["expected"] = workflow.expected_context(self, state, thread)
            context["participated"] = any(c.get("author", "").lower() == self.manifest.get("viewer", "").lower()
                                          for c in thread.get("comments", []))
        return {**state, "comments": comments, "thread_contexts": thread_contexts, "operations": active,
                "automation": self.automation_summary(), "analysis": self.analysis_summary(),
                "coverage": {"kind": self.manifest.get("analysis_kind", "full"),
                             "threads": self.manifest.get("followup_threads", [])},
                "review_status": workflow.status(self), "followup_assessments": workflow.assessments(self, state),
                "session": str(self.root), "revision": digest(state),
                "historical": self.path != current, "current_generation": generation(current, read_json(current / "manifest.json", {})),
                "pending_generation": generation(pending, read_json(pending / "manifest.json", {})) if pending else None,
                "repository": self.manifest["repository"], "pr": self.manifest["pr"], "url": self.manifest.get("url"),
                "snapshot": self.manifest.get("snapshot"), "verdicts": self.verdicts(state)}

    def automation_summary(self):
        from .automation import summary
        return summary(self)

    def analysis_summary(self):
        def describe(path):
            m = read_json(path / 'manifest.json', {})
            return dict(generation=generation(path, m), status=m.get('status'),
                        attempt_status=m.get('attempt', {}).get('status'),
                        phases={k: v.get('status') for k, v in m.get('phases', {}).items()},
                        discussion={k: m.get('discussion', {}).get(k) for k in ('complete', 'error')},
                        publication=m.get('publication', {}).get('status'))
        pending = self.pointer('.pending-review')
        return dict(displayed=describe(self.path), pending=describe(pending) if pending else None)

    def verdicts(self, state):
        pr = (state.get("checked") or {}).get("pr", read_json(self.path / "context/pr.json", {}))
        own = str(pr.get("author", {}).get("login", "")).lower() == str(self.manifest.get("viewer", "?")).lower()
        return [{"value": v, "available": v == "COMMENT" or not own,
                 "reason": "You are the PR author; GitHub permits Comment only. Inline discussions are still supported."
                 if own and v != "COMMENT" else ""}
                for v in ("COMMENT", "REQUEST_CHANGES", "APPROVE")]

    def comment_context(self, comment, state):
        placement = comment.get("placement", {})
        try:
            if placement.get("kind") == "inline":
                return self.git.context(placement)
            if placement.get("kind") == "reply":
                thread = next((t for t in state["discussion"].get("items", []) if t.get("key") == placement.get("key")), None)
                if not thread:
                    return {"error": "Saved thread is unavailable."}
                raw = thread.get("anchor", {})
                side = raw.get("side", "RIGHT")
                line = thread.get("original_line") if thread.get("is_outdated") else thread.get("line")
                start = raw.get("original_start_line") if thread.get("is_outdated") else raw.get("start_line")
                context = {"kind": "reply", "thread": thread, "path": thread.get("path"), "side": side,
                           "line": line, "start_line": start or line,
                           "commit": raw.get("original_commit_id") if thread.get("is_outdated") else self.git.head,
                           "rows": []}
                if side == "LEFT":
                    if thread.get("is_outdated"):
                        original = next((m for p in self.paths()
                                         if (m := read_json(p / "manifest.json", {})).get("snapshot", {}).get("head") == raw.get("original_commit_id")), None)
                        context["commit"] = original.get("snapshot", {}).get("merge_base") if original else None
                        if not context["commit"]:
                            context["file_unavailable"] = "The original base revision is unknown; the saved GitHub diff hunk remains available."
                    else:
                        context["commit"] = self.git.base
                # GitHub stores the original hunk for an outdated conversation.
                hunks = parse_hunks(raw.get("diff_hunk", ""))
                for h in hunks:
                    for row in h["rows"]:
                        number = row["old" if side == "LEFT" else "new"]
                        context["rows"].append({**row, "highlight": number is not None and line is not None
                                               and (start or line) <= number <= line})
                if not context["rows"] and line and not thread.get("is_outdated"):
                    context.update(self.git.context({"path": thread["path"], "side": side, "line": line}))
                    context["thread"] = thread
                return context
        except ApiError as exc:
            return {"error": str(exc)}
        return {"kind": placement.get("kind", "unplaced"), "rows": []}

    def history(self):
        result = []
        for path in self.paths():
            m = read_json(path / "manifest.json", {})
            s = Session({"session": str(self.root), "generation": generation(path, m)})
            state = s.load()
            result.append({"generation": s.id, "head": m.get("snapshot", {}).get("head"),
                           "analysis_kind": m.get("analysis_kind", "full"),
                           "created_at": m.get("created_at"), "updated_at": state.get("updated_at"),
                           "status": m.get("attempt", {}).get("status", m.get("status")),
                           "human_edits": sum(bool(c.get("human_edited") or has_manual_versions(c)) for c in state["comments"]),
                           "saved_versions": sum(len(c.get("versions", [])) for c in state["comments"]),
                           "documents": len(state.get("documents", [])), "current": path == self.current})
        return {"generations": sorted(result, key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)}


def now_from_manifest(manifest):
    # Virtual legacy imports must have a stable revision across repeated reads.
    return manifest.get("created_at", "unknown")
