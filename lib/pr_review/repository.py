"""Git and GitHub repository identity, snapshots, and publication validation."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from .common import ApiError, command, git, read_json
from .events import emit
from .storage import write_json


def fail(message):
    raise ApiError("operation_failed", message)


def repository_name(value):
    from .forge import adapter
    return adapter().project(value).repository


def normalize_remote(value):
    value = re.sub(r"^https?://", "", value) if re.match(r"^https?://", value) else (
        re.sub(r"^ssh://(?:[^@/]+@)?", "", value) if value.startswith("ssh://") else
        re.sub(r"^[^@]+@([^:]+):", r"\1/", value) if re.match(r"^[^@]+@[^:]+:", value) else "")
    return value.rstrip("/").removesuffix(".git").lower()


@dataclass
class Repository:
    cwd: Path
    common: Path
    identity: str
    pr: int
    remote: str
    forge_kind: str = "github"

    @property
    def project(self):
        from .forge import adapter
        return adapter(self.forge_kind).project(self.identity)

    @property
    def forge(self):
        from .forge import adapter
        return adapter(self.project.forge)

    @property
    def host(self):
        return self.identity.split("/", 1)[0]

    @property
    def name(self):
        return self.identity.split("/", 1)[1]

    @property
    def url(self):
        return self.forge.url(self.project, self.pr)

    @property
    def head_ref(self):
        return f"refs/pr-review/{self.project.resource_key('change', self.pr)}/head"

    @property
    def base_ref(self):
        return f"refs/pr-review/{self.project.resource_key('change', self.pr)}/base"

    @property
    def lock(self):
        return self.common / "pr-review-locks" / f"{self.project.resource_key('change', self.pr)}.lock"

    def git(self, *args, check=True, cwd=None):
        return git(args, cwd=cwd or self.cwd, check=check)

    @classmethod
    def resolve(cls, target, repository=None, cwd=None, forge=None):
        from .forge import adapter
        adapter(forge)
        cwd = Path(cwd or Path.cwd()).resolve()
        implied_project, pr = adapter(forge).target(target)
        implied = implied_project.repository if implied_project else None
        if repository:
            identity = adapter(forge).project(repository).repository
            if implied and implied != identity:
                fail("PR URL conflicts with --repo")
        elif implied:
            identity = implied_project.repository
        else:
            try:
                identity = adapter(forge).discover(cwd).repository
            except (ApiError, ValueError, KeyError) as exc:
                if isinstance(exc, ApiError) and exc.code == "cancelled":
                    raise
                fail("Cannot discover repository; specify --repo [HOST/]OWNER/REPO")
        common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd).stdout.strip())
        matches = []
        for remote in git(["remote"], cwd=cwd).stdout.splitlines():
            urls = git(["config", "--get-all", f"remote.{remote}.url"], cwd=cwd, check=False).stdout.splitlines()
            from .pulls import repository_name as remote_identity
            if any(remote_identity(url, forge) == identity for url in urls):
                matches.append(remote)
        if len(matches) != 1:
            fail(f"Expected exactly one remote for {identity}; found {len(matches)}. Check remotes and --repo.")
        return cls(cwd, common, identity, int(pr), matches[0], forge or "github")

    @classmethod
    def from_manifest(cls, manifest):
        result = git(["--git-dir", manifest["git_common_dir"], "worktree", "list", "--porcelain"])
        cwd = result.stdout.splitlines()[0].removeprefix("worktree ")
        from .forge import restore
        project = restore(manifest)
        return cls.resolve(manifest["pr"], manifest["repository"], cwd, project.forge)

    def metadata(self, destination=None):
        return self.forge.metadata(self, destination)

    def pages(self, endpoint, kind="array"):
        return self.forge.pages(self, endpoint, kind)

    def graphql(self, query, **variables):
        return self.forge.graphql(self, query, **variables)

    def verify(self, manifest, directory=None):
        snapshot = manifest["snapshot"]
        worktree = manifest["worktree"]
        valid = (self.git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == snapshot["head"]
                 and not self.git("diff", "--quiet", "HEAD", "--", cwd=worktree, check=False).returncode
                 and not self.git("diff", "--cached", "--quiet", "--", cwd=worktree, check=False).returncode
                 and self.git("rev-parse", self.head_ref).stdout.strip() == snapshot["head"]
                 and self.git("rev-parse", self.base_ref).stdout.strip() == snapshot["base"])
        if not valid:
            if directory:
                manifest["integrity_failed"] = True
                write_json(Path(directory) / "manifest.json", manifest)
            fail("Worktree HEAD, tracked source, index or snapshot refs changed; publication blocked")

    def validate_publication(self, directory, verdict="COMMENT"):
        directory = Path(directory)
        manifest = read_json(directory / "manifest.json")
        from .forge import check_account
        account = self.forge.account(self.project, self.cwd)
        check_account(manifest.get("forge_account"), account, manifest["viewer"])
        viewer = account["username"]
        if viewer.lower() != manifest["viewer"].lower():
            fail("GitHub account changed since this review; start a new review with that account")
        self.verify(manifest, directory)
        if manifest.get("integrity_failed"):
            fail("Earlier worktree integrity failure blocks publication; start a fresh run")
        pr = self.metadata(directory / "publication/pr-latest.json")
        if pr["headRefOid"] != manifest["snapshot"]["head"]:
            fail(f"PR head changed; publication blocked. Rerun pr-review {self.pr} to update this saved review.")
        if pr["state"] != "OPEN":
            fail("PR is no longer open; publication blocked")
        if verdict != "COMMENT" and pr["author"]["login"].lower() == viewer.lower():
            fail("GitHub does not allow self-approval or self-request-changes")
        ref = f"refs/pr-review/{self.project.resource_key('change', self.pr)}/publication-base"
        self.git("fetch", "--no-tags", "--refmap=", "--no-write-fetch-head", self.remote,
                 f"+refs/heads/{pr['baseRefName']}:{ref}")
        base = self.git("rev-parse", ref).stdout.strip()
        merge = self.git("merge-base", manifest["snapshot"]["head"], base).stdout.strip()
        if merge != manifest["snapshot"]["merge_base"]:
            fail(f"Merge-base changed; publication blocked. Rerun pr-review {self.pr} to update this saved review.")
        if base != manifest["snapshot"]["base"]:
            count = self.git("rev-list", "--count", manifest["snapshot"]["base"] + ".." + base).stdout.strip()
            emit("progress", message=f"Note: base has advanced {count} commits since review; reviewed snapshot is unchanged.")
