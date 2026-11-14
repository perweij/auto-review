"""Writable owner worktrees and immutable trees without touching the real index."""
import base64
import difflib
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import uuid

from .forge_context import owner_change

from .common import ApiError, digest, now
from .events import emit
from .processes import run
from .repository import normalize_remote
from .storage import hash_file


def fetch_head(session, state):
    ref = "refs/pr-owner/" + digest([session.repo.identity, session.repo.pr])[:24] + "/remote"
    session.repo.git("fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", session.repo.remote,
                     f"+{session.repo.forge.head_ref(session.repo.pr)}:{ref}")
    head = session.repo.git("rev-parse", ref).stdout.strip()
    if head != owner_change(state)['source']['head']:
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
    from .checkouts import announce
    announce(state["worktree"], session.path, "owner")
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
    if (len(matches) == 1 and not state.get("configured_target")
            and not any(session.repo.git("config", "--get", f"branch.{state['branch']}.{key}", check=False).returncode == 0
                        for key in ("remote", "merge", "pushRemote"))):
        session.repo.git("config", f"branch.{state['branch']}.remote", matches[0])
        session.repo.git("config", f"branch.{state['branch']}.merge", "refs/heads/" + target["branch"])
        state["configured_target"] = {"repository": target["repository"], "branch": target["branch"], "remote": matches[0],
                                      "method": "upstream"}
    configured = state.get("configured_target", {})
    if configured and "method" not in configured:
        # Migrate only the exact legacy mapping we installed. Magit's push-remote
        # action always uses the local branch name, not branch.<name>.merge.
        expected = {"remote": configured["remote"], "merge": "refs/heads/" + configured["branch"],
                    "pushRemote": configured["remote"]}
        if (configured["repository"] == target["repository"] and configured["branch"] == target["branch"]
                and configured["remote"] in matches
                and all(session.repo.git("config", "--get", f"branch.{state['branch']}.{key}", check=False).stdout.strip() == value
                        for key, value in expected.items())):
            before = {b["id"]: admin(session, state, b.get("git_admin")) for b in state["batches"]}
            session.repo.git("config", "--unset-all", f"branch.{state['branch']}.pushRemote")
            for batch in state["batches"]:
                if batch.get("git_admin") == before[batch["id"]]:
                    batch["git_admin"] = admin(session, state, batch["git_admin"])
        configured["method"] = "upstream"
    session.save(state)


def admin(session, state, expected=None):
    """Semantic index state avoids false alarms from Git's stat-cache refresh."""
    legacy = expected is not None and len(expected) == 64 and all(c in "0123456789abcdef" for c in expected)
    if expected is not None and not legacy and not expected.startswith("v2:"):
        raise ApiError("integrity_failed", "Unknown Git identity version; inspect and adopt before continuing.")
    worktree = state["worktree"]
    values = [session.repo.git("rev-parse", "HEAD", cwd=worktree).stdout,
              session.repo.git("symbolic-ref", "HEAD", cwd=worktree).stdout,
              session.repo.git("ls-files", "--stage", "-z", cwd=worktree).stdout]
    for path in (session.repo.common / "config", Path(worktree) / ".git"):
        values.append((hash_file(path) if path.exists() else None) if legacy or path.name != "config"
                      else configuration(session, state, path, descriptions=True))
    hooks = session.repo.common / "hooks"
    if hooks.exists():
        values.extend((str(p.relative_to(hooks)), os.readlink(p) if p.is_symlink() else hash_file(p))
                      for p in sorted(hooks.rglob("*")) if p.is_file() or p.is_symlink())
    gitdir = Path(session.repo.git("rev-parse", "--absolute-git-dir", cwd=worktree).stdout.strip())
    config = gitdir / "config.worktree"
    values.append(hash_file(config) if config.exists() else None)
    if not legacy:
        values.append(configuration(session, state, config, descriptions=False))
    return digest(values) if legacy else "v2:" + digest(values)


def configuration(session, state, path, *, descriptions):
    """Ordered Git records, with one narrow shared-config metadata exemption."""
    if not path.exists():
        return None
    result = session.repo.git("config", "--file", str(path), "--includes", "--null",
                              "--list", "--show-origin", cwd=state["worktree"], check=False)
    if result.returncode or "\ufffd" in result.stdout:
        raise ApiError("integrity_failed", "Cannot read protected Git configuration.")
    fields = result.stdout.split("\0")
    if fields.pop() != "" or len(fields) % 2:
        raise ApiError("integrity_failed", "Cannot parse protected Git configuration.")
    records = list(zip(fields[::2], fields[1::2]))
    metadata = state.get("metadata", {})
    protected = {state.get("branch"), metadata.get("headRefName"), metadata.get("baseRefName"),
                 state.get("push_target", {}).get("branch")}
    known = all(isinstance(name, str) and name for name in protected)
    # Resolve upstream names from the effective configuration, including worktree
    # overrides. This only makes the exemption narrower; global settings are not
    # otherwise added to the protected identity.
    upstream_records = []
    if descriptions and known:
        effective = session.repo.git("config", "--null", "--list", "--includes",
                                     cwd=state["worktree"], check=False)
        if effective.returncode or "\ufffd" in effective.stdout:
            raise ApiError("integrity_failed", "Cannot determine protected Git branches.")
        upstream_records = effective.stdout.split("\0")[:-1]
    changed = True
    while changed:
        changed = False
        for record in upstream_records:
            key, sep, value = record.partition("\n")
            for branch in tuple(protected):
                if branch and key == "branch." + branch + ".merge" and sep and value.startswith("refs/heads/"):
                    upstream = value[len("refs/heads/"):]
                    if upstream not in protected:
                        protected.add(upstream)
                        changed = True
    kept, included = [], {}
    for origin, record in records:
        if not origin.startswith("file:"):
            raise ApiError("integrity_failed", "Unexpected Git configuration origin.")
        source = Path(origin[5:])
        if not source.is_absolute():
            source = Path(state["worktree"]) / source
        shared = source.resolve() == path.resolve()
        key = record.partition("\n")[0]
        if (descriptions and known and shared and key.startswith("branch.")
                and key.endswith(".description") and key[7:-12] not in protected):
            continue
        kept.append((origin, record))
        if not shared:
            try:
                included[str(source)] = hash_file(source)
            except OSError as exc:
                raise ApiError("integrity_failed", "Cannot read included Git configuration.") from exc
    return [kept, sorted(included.items())]


def checkpoint(session, state, label):
    validate(session, state)
    # A superproject tree records submodule commits, not edits inside them.
    # Refuse such edits rather than certifying a checkpoint that omits them.
    entries = session.repo.git('ls-files', '--stage', '-z', cwd=state['worktree']).stdout.split('\0')
    for entry in entries:
        if not entry.startswith('160000 '):
            continue
        name = entry.split('\t', 1)[1]
        module = Path(state['worktree']) / name
        if (module / '.git').exists() and session.repo.git(
                'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none', cwd=module).stdout:
            raise ApiError('dirty_submodule', f'Cannot snapshot uncommitted changes inside submodule {name}. Inspect and commit or reconcile them before continuing; retained files were not changed.')
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
    ignored = [p for p in ignored if p]
    return {"tree": tree, "ref": ref, "label": label, "at": now(),
            "ignored": ignored[:20], "ignored_count": len(ignored)}


def pin_commit(session, sha):
    """Retain provenance even if a human later rewrites all public branch refs."""
    ref = f"refs/pr-owner/{digest(str(session.path))[:24]}/commits/{sha}"
    session.repo.git("update-ref", ref, sha)
    return ref


REVIEW_COMMIT_MESSAGE = "Review candidate\n\nInternal pr-review artifact bridging a checkpoint tree to commit-based review.\nIt is not an implementation commit and is never pushed.\n"
REVIEW_COMMIT_ENV = {"GIT_AUTHOR_NAME": "pr-review", "GIT_AUTHOR_EMAIL": "pr-review@invalid",
                     "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z", "GIT_COMMITTER_NAME": "pr-review",
                     "GIT_COMMITTER_EMAIL": "pr-review@invalid", "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z"}


def review_commit(session, tree, baseline):
    """Bridge an existing checkpoint tree to the commit-based review machinery.

    The commit is a deterministic internal review artifact: the same tree and
    baseline always give the same SHA, whatever the time or the user's Git
    configuration. It is unreachable from the Owner branch, never touches HEAD or
    the index and is never pushed. Acceptance still binds to the checkpoint tree.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    env.update(REVIEW_COMMIT_ENV)
    result = run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                  "-c", "i18n.commitEncoding=UTF-8", "commit-tree", tree, "-p", baseline,
                  "-m", REVIEW_COMMIT_MESSAGE], cwd=session.repo.cwd, env=env)
    sha = result.stdout.strip()
    if result.returncode or not sha:
        raise ApiError("review_candidate_failed", result.stderr.strip() or "Cannot create the review candidate commit.")
    # The SHA covers both tree and baseline, so another baseline gets another ref.
    ref = f"refs/pr-owner/{digest(str(session.path))[:24]}/review-candidates/{sha}"
    session.repo.git("update-ref", ref, sha)
    return {"sha": sha, "ref": ref, "tree": tree, "baseline": baseline}


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


def new_batch_head(session, state, remote):
    """Bring a clean, behind owner branch forward before recording a new batch."""
    clean(session, state)
    worktree = state["worktree"]
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = session.repo.git("rev-parse", "--path-format=absolute", "--git-path", name, cwd=worktree).stdout.strip()
        if Path(path).exists():
            raise ApiError("git_in_progress", "A Git operation is in progress; finish it in Magit before starting a fix.")
    head = session.repo.git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    if ancestor(session, remote, head):
        return head
    if not ancestor(session, head, remote):
        raise ApiError("diverged", "Owner and GitHub branches have different commits; use C-u m to reconcile in Magit, then start a new fix.")
    from .owner_state import superseded_batches
    superseded = superseded_batches(state)
    if any(b["baseline"] == head and b["status"] != "ready" and b["id"] not in superseded for b in state["batches"]):
        raise ApiError("worktree_behind", "Owner worktree is behind GitHub with an unfinished batch; use C-u m to inspect and reconcile it before starting a new fix.")
    emit("progress", message=f"Owner worktree is behind GitHub; fast-forwarding {head[:12]} to {remote[:12]} before starting the new fix.")
    session.repo.git("merge", "--ff-only", "--no-squash", "--no-autostash", "--no-overwrite-ignore", remote, cwd=worktree)
    validate(session, state)
    clean(session, state)
    if session.repo.git("rev-parse", "HEAD", cwd=worktree).stdout.strip() != remote:
        raise ApiError("stale_source", "Owner HEAD changed during synchronization; inspect it in Magit and retry.")
    emit("progress", message=f"Owner worktree updated to {remote[:12]}; starting the new fix from this commit.")
    return remote


def commit_records(session, baseline, head):
    """Read reachable commit identities, trees, and parents in one Git call."""
    records = session.repo.git("log", "--no-patch", "--no-show-signature", "--format=%H %T %P",
                               baseline + ".." + head, "--").stdout.splitlines()
    return [(fields[0], fields[1], fields[2:]) for line in records if len(fields := line.split()) >= 2]


def exact_commits(session, batch, head):
    """Find reachable commits with the verified tree and sole baseline parent."""
    return [sha for sha, tree, parents in commit_records(session, batch["baseline"], head)
            if tree == batch["final_tree"] and parents == [batch["baseline"]]]


def diff(session, before, after):
    return session.repo.git("diff", "--no-ext-diff", "--no-textconv", "--binary", before, after, "--").stdout


def agent_configuration(path):
    path = Path(path)
    return (path.name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", ".mcp.json")
            or any(part in (".claude", ".codex") for part in path.parts))


def configuration_snapshot(worktree):
    """Capture configuration independently of Git's index and ignore rules."""
    root = Path(worktree)
    result = {}

    def visit(directory, ancestors):
        info = directory.stat()
        identity = (info.st_dev, info.st_ino)
        if identity in ancestors:
            if agent_configuration(directory.relative_to(root)):
                raise OSError(f"Directory cycle at {directory}")
            # This ancestor is already being scanned. Unrelated symlink cycles
            # add no configuration; configuration paths must still fail closed.
            return
        for path in sorted(directory.iterdir()):
            if path.name == ".git":
                continue
            if path.is_dir():
                visit(path, ancestors | {identity})
                continue
            name = path.relative_to(root).as_posix()
            if not agent_configuration(name):
                continue
            if not path.is_file():
                raise OSError(f"Configuration is not a readable regular file: {path}")
            result[name] = {"content": base64.b64encode(path.read_bytes()).decode("ascii"),
                            "mode": stat.S_IMODE(path.stat().st_mode),
                            "link": os.readlink(path) if path.is_symlink() else None}

    try:
        visit(root, set())
    except OSError as exc:
        raise ApiError("configuration_unreadable", f"Cannot inspect agent configuration: {exc}") from exc
    return result


def configuration_diff(before, after, paths):
    patches = []
    for path in paths:
        old, new = before.get(path), after.get(path)
        patches.append(f"diff --agent-config a/{path} b/{path}\n")
        if old is None or new is None:
            patches.append("Configuration added\n" if old is None else "Configuration deleted\n")
        for key in ("mode", "link"):
            if (old or {}).get(key) != (new or {}).get(key):
                patches.append(f"{key}: {(old or {}).get(key)!r} -> {(new or {}).get(key)!r}\n")
        contents = [base64.b64decode(value["content"]) if value else b"" for value in (old, new)]
        try:
            texts = [content.decode("utf-8") for content in contents]
            if any("\0" in text for text in texts):
                raise ValueError("Binary configuration")
        except (UnicodeError, ValueError):
            patches.append(f"Binary content (base64): {(old or {}).get('content', '')} -> {(new or {}).get('content', '')}\n")
        else:
            lines = difflib.unified_diff(texts[0].splitlines(keepends=True), texts[1].splitlines(keepends=True),
                                         fromfile=f"a/{path}" if old else "/dev/null",
                                         tofile=f"b/{path}" if new else "/dev/null")
            for line in lines:
                patches.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(patches)


def trust(session, state, tree, approval=None):
    base = session.repo.git("merge-base", owner_change(state)['target']['head'], owner_change(state)['source']['head'], check=False)
    # Fetch the recorded base object without moving a caller's tracking refs.
    if base.returncode:
        session.repo.git("fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", session.repo.remote, owner_change(state)['target']['head'])
        base = session.repo.git("merge-base", owner_change(state)['target']['head'], owner_change(state)['source']['head'])
    changed = session.repo.git("diff", "--name-only", "--no-renames", "-z", base.stdout.strip(), tree, "--").stdout.split("\0")
    paths = sorted(p for p in changed if p and agent_configuration(p))
    configuration = configuration_snapshot(state["worktree"])
    previous = state.get("trusted_configuration")
    if previous is None and not state.get("trusted_snapshot") and not approval:
        # A new session inherits unchanged, tracked base configuration. Local,
        # ignored, linked, or PR-modified instructions still need confirmation.
        previous = {}
        for path, value in configuration.items():
            entry = session.repo.git("ls-tree", base.stdout.strip(), "--", path).stdout
            fields = entry.split("\t", 1)[0].split()
            if len(fields) != 3 or fields[0] not in ("100644", "100755") or fields[1] != "blob" or value["link"] is not None:
                continue
            mode, _, oid = fields
            content = base64.b64decode(value["content"])
            blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
            actual = hashlib.new("sha256" if len(oid) == 64 else "sha1", blob).hexdigest()
            if actual == oid and bool(value["mode"] & 0o111) == (mode == "100755"):
                previous[path] = value
    # Source edits do not revoke trust in unchanged configuration content.
    token = digest({"version": 2, "configuration": configuration,
                    "tree_entries": [(p, session.repo.git("ls-tree", tree, "--", p).stdout) for p in paths]})
    if approval == token:
        state.update(trusted_snapshot=token, trusted_configuration=configuration)
        session.save(state)
        return
    if previous == configuration and state.get("trusted_snapshot") == token and not approval:
        return
    config_paths = sorted(p for p in set(previous or {}) | set(configuration)
                          if (previous or {}).get(p) != configuration.get(p))
    # Legacy tree-only approvals cannot authorize a configuration snapshot.
    if paths or config_paths or state.get("trusted_snapshot") or approval:
        patch = (session.repo.git("diff", "--no-ext-diff", "--no-textconv", base.stdout.strip(), tree, "--", *paths).stdout
                 if paths else "")
        patch += configuration_diff(previous or {}, configuration, config_paths)
        raise ApiError("trust_required", "Inspect changed agent configuration before continuing.",
                       trusted_snapshot=token, paths=sorted(set(paths) | set(config_paths)), diff=patch)
    state.update(trusted_snapshot=token, trusted_configuration=configuration)
    session.save(state)
