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
    parts = value.split("/")
    if len(parts) == 2:
        parts.insert(0, "github.com")
    if (len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parts[0])
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in (".", "..")
                   or p.startswith("-") for p in parts[1:])):
        fail("Repository must be [HOST/]OWNER/REPO")
    return "/".join(parts).lower()


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

    @property
    def host(self):
        return self.identity.split("/", 1)[0]

    @property
    def name(self):
        return self.identity.split("/", 1)[1]

    @property
    def url(self):
        return f"https://{self.identity}/pull/{self.pr}"

    @property
    def head_ref(self):
        return f"refs/pr-review/{self.pr}/head"

    @property
    def base_ref(self):
        return f"refs/pr-review/{self.pr}/base"

    @property
    def lock(self):
        return self.common / "pr-review-locks" / f"{self.pr}.lock"

    def git(self, *args, check=True, cwd=None):
        return git(args, cwd=cwd or self.cwd, check=check)

    @classmethod
    def resolve(cls, target, repository=None, cwd=None):
        cwd = Path(cwd or Path.cwd()).resolve()
        target = str(target)
        match = re.fullmatch(r"https://([A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", target)
        implied = match[1].lower() if match else None
        pr = match[2] if match else target
        if not re.fullmatch(r"[1-9][0-9]*", pr):
            fail("Expected a positive PR number or an https://HOST/OWNER/REPO/pull/NUMBER URL")
        if repository:
            identity = repository_name(repository)
            if implied and implied != identity:
                fail("PR URL conflicts with --repo")
        elif implied:
            identity = repository_name(implied)
        else:
            try:
                result = command(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd=cwd)
                identity = repository_name(normalize_remote(json.loads(result.stdout)["url"]))
            except (ApiError, ValueError, KeyError) as exc:
                if isinstance(exc, ApiError) and exc.code == "cancelled":
                    raise
                fail("Cannot discover repository; specify --repo [HOST/]OWNER/REPO")
        common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd).stdout.strip())
        matches = []
        for remote in git(["remote"], cwd=cwd).stdout.splitlines():
            urls = git(["config", "--get-all", f"remote.{remote}.url"], cwd=cwd, check=False).stdout.splitlines()
            if any(normalize_remote(url) == identity for url in urls):
                matches.append(remote)
        if len(matches) != 1:
            fail(f"Expected exactly one remote for {identity}; found {len(matches)}. Check remotes and --repo.")
        return cls(cwd, common, identity, int(pr), matches[0])

    @classmethod
    def from_manifest(cls, manifest):
        result = git(["--git-dir", manifest["git_common_dir"], "worktree", "list", "--porcelain"])
        cwd = result.stdout.splitlines()[0].removeprefix("worktree ")
        return cls.resolve(manifest["pr"], manifest["repository"], cwd)

    def metadata(self, destination=None):
        fields = "number,title,body,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,author"
        try:
            value = json.loads(command(["gh", "pr", "view", str(self.pr), "-R", self.identity,
                                        "--json", fields], cwd=self.cwd).stdout)
        except (ApiError, ValueError) as exc:
            if isinstance(exc, ApiError) and exc.code == "cancelled":
                raise
            fail("Repository or PR inaccessible")
        if (not isinstance(value, dict) or str(value.get("number")) != str(self.pr)
                or str(value.get("url", "")).lower().rstrip("/") != self.url
                or any(not re.fullmatch(r"[0-9a-f]{40,64}", str(value.get(k, "")))
                       for k in ("headRefOid", "baseRefOid"))
                or not isinstance(value.get("baseRefName"), str) or not value["baseRefName"]
                or not isinstance(value.get("author"), dict) or not isinstance(value["author"].get("login"), str)
                or value.get("state") not in ("OPEN", "CLOSED", "MERGED")):
            fail("PR metadata identity or snapshot is invalid")
        self.git("check-ref-format", "refs/heads/" + value["baseRefName"])
        if destination:
            write_json(destination, value)
        return value

    def pages(self, endpoint, kind="array"):
        result = command(["gh", "api", "--hostname", self.host, "--paginate", "--slurp", endpoint], cwd=self.cwd)
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list):
                raise ValueError()
            if kind == "actions":
                pages = [p["workflow_runs"] for p in pages]
            if any(not isinstance(p, list) for p in pages):
                raise ValueError()
            return [item for page in pages for item in page]
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError("invalid_response", "Incomplete GitHub pages") from exc

    def graphql(self, query, **variables):
        # Keep the existing gh query/field convention and authentication setup.
        args = ["gh", "api", "--hostname", self.host, "graphql", "-f", "query=" + query]
        for name, value in variables.items():
            args += ["-F" if type(value) is int else "-f", f"{name}={value}"]
        try:
            value = json.loads(command(args, cwd=self.cwd).stdout)
            if not isinstance(value, dict) or value.get("errors"):
                raise ValueError()
            return value["data"]
        except (ValueError, KeyError) as exc:
            raise ApiError("invalid_response", "Incomplete GraphQL response") from exc

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
        viewer = command(["gh", "api", "--hostname", self.host, "user", "--jq", ".login"], cwd=self.cwd).stdout.strip()
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
        ref = f"refs/pr-review/{self.pr}/publication-base"
        self.git("fetch", "--no-tags", "--refmap=", "--no-write-fetch-head", self.remote,
                 f"+refs/heads/{pr['baseRefName']}:{ref}")
        base = self.git("rev-parse", ref).stdout.strip()
        merge = self.git("merge-base", manifest["snapshot"]["head"], base).stdout.strip()
        if merge != manifest["snapshot"]["merge_base"]:
            fail(f"Merge-base changed; publication blocked. Rerun pr-review {self.pr} to update this saved review.")
        if base != manifest["snapshot"]["base"]:
            count = self.git("rev-list", "--count", manifest["snapshot"]["base"] + ".." + base).stdout.strip()
            emit("progress", message=f"Note: base has advanced {count} commits since review; reviewed snapshot is unchanged.")
