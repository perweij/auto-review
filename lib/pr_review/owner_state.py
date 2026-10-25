"""Owner sessions and local triage, independent of reviewer generations."""
import copy
import re
from pathlib import Path

from .common import ApiError, digest, git, locked, now, read_json, require_text, state_root
from .repository import Repository, normalize_remote, repository_name
from .settings import DEFAULTS, validate as validate_reviewer_settings
from .storage import write_json


def root():
    return state_root() / "owners-v1"


def default_settings(request):
    defaults = request.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ApiError("invalid_request", "Owner defaults must be a settings object.")
    resolved = validate_reviewer_settings({**DEFAULTS, **{k: v for k, v in defaults.items() if k in DEFAULTS and v is not None}})
    provider = resolved["processing_provider"]
    value = {"provider": provider, "model": resolved[provider + "_model"],
             "effort": resolved[provider + "_effort"]}
    return {"fix": dict(value), "review": dict(value), "verification": "focused"}


def validate_settings(value):
    if not isinstance(value, dict) or value.get("verification") != "focused":
        raise ApiError("invalid_request", "Owner verification must be focused.")
    result = {"verification": "focused"}
    for role in ("fix", "review"):
        choice = value.get(role)
        if not isinstance(choice, dict) or choice.get("provider") not in ("claude", "codex"):
            raise ApiError("invalid_request", "Choose Claude or Codex for each owner role.")
        result[role] = {key: require_text(choice.get(key), key) for key in ("provider", "model", "effort")}
        if any(v.startswith("-") or any(ord(c) < 32 for c in v) for v in result[role].values()):
            raise ApiError("invalid_request", "Invalid owner provider setting.")
    return result


def local_repository(request):
    cwd = Path(request.get("cwd", ".")).resolve()
    identity = request.get("repository")
    target = str(request.get("pr", ""))
    match = re.fullmatch(r"https://([^/]+/[^/]+/[^/]+)/pull/([1-9][0-9]*)/?", target)
    if not identity and not match:
        remotes = git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=cwd, check=False)
        candidates = {normalize_remote(line.split(None, 1)[1]) for line in remotes.stdout.splitlines()}
        candidates.discard("")
        if len(candidates) != 1:
            raise ApiError("invalid_request", "Choose a repository from this checkout's remotes.")
        identity = candidates.pop()
    return Repository.resolve(target, identity, cwd)


def open_session(request):
    repo = local_repository(request)  # Explicit identity: no GitHub read on open.
    worktrees = [Path(line[9:]).resolve() for line in repo.git("worktree", "list", "--porcelain").stdout.splitlines()
                 if line.startswith("worktree ")]
    if any(root().is_relative_to(path) for path in worktrees):
        raise ApiError("invalid_session", "Artifacts must be outside repository worktrees; set XDG_STATE_HOME.")
    path = root() / digest([str(repo.common), repo.identity])[:32] / f"pr-{repo.pr}"
    with locked(path / ".state.lock"):
        if not (path / "session.json").exists():
            write_json(path / "session.json", {
                "kind": "owner", "format_version": 1, "sequence": 0, "created_at": now(),
                "repository": repo.identity, "pr": repo.pr, "url": repo.url,
                "cwd": str(repo.cwd), "git_common_dir": str(repo.common),
                "settings": default_settings(request), "checked_at": None, "viewer": None,
                "feedback": [], "batches": [], "replies": [], "actions": [], "worktree": None})
    return OwnerSession({"session": str(path)}).view()


def discover(request):
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=request.get("cwd", ".")).stdout.strip()
    values = []
    for file in root().glob("*/pr-*/session.json"):
        value = read_json(file, {})
        if value.get("git_common_dir") != common:
            continue
        if request.get("repository") and value.get("repository") != repository_name(request["repository"]):
            continue
        if request.get("pr") and str(value.get("pr")) != str(request["pr"]):
            continue
        values.append({k: value.get(k) for k in ("repository", "pr", "url", "checked_at", "created_at")})
        values[-1]["session"] = str(file.parent)
    return {"sessions": values}


def toolkit(request):
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=request.get("cwd", ".")).stdout.strip()
    identity = request.get("repository")
    if not identity:
        remotes = git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=request.get("cwd", "."), check=False)
        names = {normalize_remote(line.split(None, 1)[1]) for line in remotes.stdout.splitlines()}
        names.discard("")
        if len(names) == 1:
            identity = names.pop()
    if identity:
        identity = repository_name(identity)
    path = state_root() / "toolkits" / (digest([common, identity]) + ".json")
    with locked(path.with_suffix(".lock")):
        if "toolkit" in request:
            if request["toolkit"] not in ("reviewer", "owner"):
                raise ApiError("invalid_request", "Choose Reviewer or Owner.")
            write_json(path, {"toolkit": request["toolkit"]})
        return read_json(path, {"toolkit": "reviewer"})


def fingerprint(item, viewer=None):
    comments = item.get("comments", [])
    if viewer:
        comments = [c for c in comments if c.get("author", "").lower() != viewer.lower()]
    return digest([item["key"], comments])


class OwnerSession:
    def __init__(self, request):
        self.path = Path(require_text(request.get("session"), "session")).resolve()
        if not self.path.is_relative_to(root().resolve()):
            raise ApiError("invalid_session", "Choose a saved owner session.")
        self.lock = self.path / ".state.lock"
        self.file = self.path / "session.json"
        self.manifest = self.load()
        self.repo = Repository.resolve(self.manifest["pr"], self.manifest["repository"], self.manifest["cwd"])
        if str(self.repo.common) != self.manifest["git_common_dir"]:
            raise ApiError("invalid_session", "Owner checkout identity changed.")
        self.execution_lock = self.repo.common / "pr-owner-locks" / (digest([self.repo.identity, self.repo.pr]) + ".lock")

    def load(self):
        value = read_json(self.file)
        if not isinstance(value, dict) or value.get("kind") != "owner" or value.get("format_version") != 1:
            raise ApiError("invalid_session", "Unsupported or missing owner session.")
        return value

    def check(self, request, state):
        if request.get("revision") != state["sequence"]:
            raise ApiError("stale_revision", "Owner state changed; reload before retrying.")

    def save(self, state):
        # Mutating use cases hold execution_lock; state locks are short.
        with locked(self.lock):
            current = self.load()
            if current["sequence"] != state["sequence"]:
                raise ApiError("stale_revision", "Owner state changed; saved work was retained.")
            state["sequence"] += 1
            state["updated_at"] = now()
            write_json(self.file, state)
            self.manifest = state

    def view(self, state=None):
        result = copy.deepcopy(state or self.load())
        result.update(session=str(self.path), revision=result.pop("sequence"))
        return result

    def items(self, state, keys):
        if not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise ApiError("invalid_request", "Select distinct feedback items.")
        found = {i["key"]: i for i in state["feedback"]}
        if any(k not in found or found[k].get("missing") for k in keys):
            raise ApiError("invalid_target", "Feedback is no longer available; refresh first.")
        return [found[k] for k in keys]


def refresh(session, state):
    """Fetch first, then atomically replace local context; failure retains cache."""
    from .discussion import fetch
    from .pulls import github_json
    repo = session.repo
    metadata = repo.metadata()
    raw = github_json(repo.identity, f"repos/{repo.name}/pulls/{repo.pr}", repo.cwd)
    if raw.get("number") != repo.pr or (raw.get("html_url") or "").lower() != repo.url:
        raise ApiError("invalid_response", "GitHub returned a different owner PR.")
    head = raw.get("head") or {}
    if head.get("sha") != metadata["headRefOid"]:
        raise ApiError("stale_source", "PR changed during refresh; retry.")
    head_repo = head.get("repo") or {}
    target = normalize_remote(head_repo.get("html_url", ""))
    if target:
        target = repository_name(target)
    branch = require_text(head.get("ref"), "head branch")
    repo.git("check-ref-format", "refs/heads/" + branch)
    viewer_data = github_json(repo.identity, "user", repo.cwd)
    viewer = require_text(viewer_data.get("login"), "GitHub account")
    if state.get("viewer") and state["viewer"].lower() != viewer.lower():
        raise ApiError("account_changed", "GitHub account changed; return to the owner session's original account.")
    discussion, raw_discussion = fetch(repo)
    anchors = {c["id"]: c for c in raw_discussion["inline"]}
    previous = {i["key"]: i for i in state["feedback"]}
    latest = {}
    for item in discussion["items"]:
        if item["kind"] == "review" and item.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            c = item["comments"][0]
            author = c["author"].lower()
            prior = latest.get(author)
            if not prior or (c["created_at"], c["id"]) > prior[0]:
                latest[author] = ((c["created_at"], c["id"]), item["key"])
    items, changed = [], set()
    for item in discussion["items"]:
        old = previous.pop(item["key"], {})
        if item["kind"] == "thread":
            item["diff_hunk"] = anchors.get(item["root_id"], {}).get("diff_hunk", "")
        incoming = [c for c in item["comments"] if c["author"].lower() != viewer.lower() and c["body"].strip()]
        notification = fingerprint(item, viewer)
        content = fingerprint(item)
        if old.get("fingerprint") != content:
            changed.add(item["key"])
        item.update(fingerprint=content, notification=notification,
                    triage=old.get("triage", "attention" if incoming else "handled"),
                    unread=old.get("unread", bool(incoming)))
        if old and notification != old.get("notification") and incoming:
            item.update(triage="attention", unread=True)
        item["actionable"] = bool(incoming)
        item["change_requested"] = (item.get("state") == "CHANGES_REQUESTED" and
            latest.get(item["comments"][0]["author"].lower(), (None, None))[1] == item["key"])
        if item["change_requested"] and not old:
            item["triage"] = "attention"
        item["attention"] = item["triage"] == "attention" and bool(
            item["unread"] or item["change_requested"] or
            (item["kind"] == "thread" and not item["is_resolved"]))
        items.append(item)
    for old in previous.values():
        old.update(missing=True, attention=False)
        items.append(old)
        changed.add(old["key"])
    for reply in state["replies"]:
        if reply["status"] != "published" and reply["source"] in changed:
            reply.update(status="unstaged", needs_attention="Discussion changed; inspect and acknowledge before staging.")
    for batch in state["batches"]:
        if set(batch["items"]) & changed:
            batch["discussion_stale"] = True
    push_target = {"repository": target, "branch": branch}
    previous_target = state.get("push_target", {})
    if all(previous_target.get(k) == v for k, v in push_target.items()) and "remotes" in previous_target:
        push_target["remotes"] = previous_target["remotes"]
    state.update(feedback=items, metadata=metadata, viewer=viewer, checked_at=now(), push_target=push_target)
    snapshot = session.path / "context" / (digest([metadata, discussion]) + ".json")
    write_json(snapshot, {"metadata": metadata, "discussion": discussion, "checked_at": state["checked_at"]})
    state["context"] = str(snapshot)
    session.save(state)
    return changed
