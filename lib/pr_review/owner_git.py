"""Writable owner worktrees and immutable trees without touching the real index."""
import os
from pathlib import Path
import tempfile
import uuid

from .common import ApiError, digest, now
from .processes import run
from .repository import normalize_remote
from .storage import hash_file


def fetch_head(session, state):
    ref = "refs/pr-owner/" + digest([session.repo.identity, session.repo.pr])[:24] + "/remote"
    session.repo.git("fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", session.repo.remote,
                     f"+refs/pull/{session.repo.pr}/head:{ref}")
    head = session.repo.git("rev-parse", ref).stdout.strip()
    if head != state["metadata"]["headRefOid"]:
        raise ApiError("stale_source", "PR head changed during fetch; refresh and retry.")
    return head


def prepare(session, state):
    if state["metadata"]["state"] != "OPEN":
        raise ApiError("closed_pr", "Fixes require an open PR.")
    head = fetch_head(session, state)
    if not state.get("worktree"):
        base = Path(session.repo.git("worktree", "list", "--porcelain").stdout.splitlines()[0][9:])
        identity = digest([session.repo.identity, session.repo.pr])[:10]
        state.update(worktree=str(base.parent / f"{base.name}-owner-pr-{session.repo.pr}-{identity}"),
                     branch=f"pr-owner/{session.repo.pr}-{identity}", setup={"head": head, "status": "pending"})
        session.save(state)
    worktree = Path(state["worktree"])
    if session.path.is_relative_to(worktree.resolve()):
        raise ApiError("invalid_session", "Owner artifacts must be outside its worktree; set XDG_STATE_HOME.")
    if state.get("setup", {}).get("status") == "pending":
        if not worktree.exists():
            ref = session.repo.git("show-ref", "--verify", "--quiet", "refs/heads/" + state["branch"], check=False)
            if ref.returncode == 0:
                raise ApiError("branch_collision", "Owner branch already exists; inspect the saved setup journal.")
            session.repo.git("worktree", "add", "-b", state["branch"], str(worktree), state["setup"]["head"])
        validate(session, state)
        if session.repo.git("rev-parse", "HEAD", cwd=worktree).stdout.strip() != state["setup"]["head"]:
            raise ApiError("integrity_failed", "Interrupted owner setup has a different HEAD.")
        state["setup"]["status"] = "complete"
        session.save(state)
    validate(session, state)
    configure_push(session, state)
    return head


def validate(session, state):
    worktree = state.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        raise ApiError("missing_worktree", "Owner worktree is missing; inspect the saved session.")
    common = session.repo.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree).stdout.strip()
    branch = session.repo.git("symbolic-ref", "--quiet", "HEAD", cwd=worktree, check=False).stdout.strip()
    if common != str(session.repo.common) or branch != "refs/heads/" + state["branch"]:
        raise ApiError("integrity_failed", "Owner worktree identity or branch changed; reconcile manually.")


def configure_push(session, state):
    target = state["push_target"]
    matches = []
    for remote in session.repo.git("remote").stdout.splitlines():
        urls = session.repo.git("remote", "get-url", "--push", "--all", remote).stdout.splitlines()
        if urls and all(normalize_remote(url) == target["repository"] for url in urls):
            matches.append(remote)
    target["remotes"] = matches
    if len(matches) == 1 and not state.get("configured_target"):
        session.repo.git("config", f"branch.{state['branch']}.remote", matches[0])
        session.repo.git("config", f"branch.{state['branch']}.merge", "refs/heads/" + target["branch"])
        session.repo.git("config", f"branch.{state['branch']}.pushRemote", matches[0])
        state["configured_target"] = {"repository": target["repository"], "branch": target["branch"], "remote": matches[0]}
    session.save(state)


def admin(session, state):
    """Semantic index state avoids false alarms from Git's stat-cache refresh."""
    worktree = state["worktree"]
    values = [session.repo.git("rev-parse", "HEAD", cwd=worktree).stdout,
              session.repo.git("symbolic-ref", "HEAD", cwd=worktree).stdout,
              session.repo.git("ls-files", "--stage", "-z", cwd=worktree).stdout]
    for path in (session.repo.common / "config", Path(worktree) / ".git"):
        values.append(hash_file(path) if path.exists() else None)
    hooks = session.repo.common / "hooks"
    if hooks.exists():
        values.extend((str(p.relative_to(hooks)), os.readlink(p) if p.is_symlink() else hash_file(p))
                      for p in sorted(hooks.rglob("*")) if p.is_file() or p.is_symlink())
    gitdir = Path(session.repo.git("rev-parse", "--absolute-git-dir", cwd=worktree).stdout.strip())
    config = gitdir / "config.worktree"
    values.append(hash_file(config) if config.exists() else None)
    return digest(values)


def checkpoint(session, state, label):
    validate(session, state)
    with tempfile.TemporaryDirectory(prefix="index-", dir=session.path) as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index"), "GIT_OPTIONAL_LOCKS": "0"}
        def git(*args):
            result = run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=state["worktree"], env=env)
            if result.returncode:
                raise ApiError("checkpoint_failed", result.stderr or "Cannot snapshot owner worktree.")
            return result.stdout.strip()
        git("read-tree", "HEAD")
        git("add", "-A", "--", ".")
        tree = git("write-tree")
    ref = f"refs/pr-owner/{digest(str(session.path))[:24]}/checkpoints/{uuid.uuid4().hex}"
    session.repo.git("update-ref", ref, tree)
    ignored = session.repo.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=state["worktree"]).stdout.split("\0")
    return {"tree": tree, "ref": ref, "label": label, "at": now(), "ignored": [p for p in ignored if p]}


def pin_commit(session, sha):
    """Retain provenance even if a human later rewrites all public branch refs."""
    ref = f"refs/pr-owner/{digest(str(session.path))[:24]}/commits/{sha}"
    session.repo.git("update-ref", ref, sha)
    return ref


def clean(session, state):
    if session.repo.git("status", "--porcelain=v1", "--untracked-files=all", cwd=state["worktree"]).stdout:
        raise ApiError("dirty_worktree", "Start a new batch from a clean owner worktree; commit or reconcile in Magit.")


def unstaged_index(session, state):
    if session.repo.git("diff", "--cached", "--quiet", "HEAD", "--", cwd=state["worktree"], check=False).returncode:
        raise ApiError("staged_changes", "The owner index has staged changes; inspect and unstage them in Magit before resuming.")


def ancestor(session, older, newer):
    code = session.repo.git("merge-base", "--is-ancestor", older, newer, check=False).returncode
    if code not in (0, 1):
        raise ApiError("invalid_commit", "Cannot check commit ancestry.")
    return code == 0


def diff(session, before, after):
    return session.repo.git("diff", "--no-ext-diff", "--no-textconv", "--binary", before, after, "--").stdout


def trust(session, state, tree):
    base = session.repo.git("merge-base", state["metadata"]["baseRefOid"], state["metadata"]["headRefOid"], check=False)
    # Fetch the recorded base object without moving a caller's tracking refs.
    if base.returncode:
        session.repo.git("fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", session.repo.remote, state["metadata"]["baseRefOid"])
        base = session.repo.git("merge-base", state["metadata"]["baseRefOid"], state["metadata"]["headRefOid"])
    changed = session.repo.git("diff", "--name-only", "--no-renames", "-z", base.stdout.strip(), tree, "--").stdout.split("\0")
    paths = [p for p in changed if p and (Path(p).name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json")
             or any(part in (".claude", ".codex") for part in Path(p).parts))]
    # Source edits do not revoke trust in unchanged configuration content.
    token = digest([(p, session.repo.git("ls-tree", tree, "--", p).stdout) for p in paths])
    if paths and state.get("trusted_snapshot") != token:
        patch = session.repo.git("diff", "--no-ext-diff", "--no-textconv", base.stdout.strip(), tree, "--", *paths).stdout
        raise ApiError("trust_required", "Inspect changed agent configuration before continuing.",
                       trusted_snapshot=token, paths=paths, diff=patch)
