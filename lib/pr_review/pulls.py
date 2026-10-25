"""Repository PR discovery, separate from saved review generation discovery."""
import json
import re

from .common import ApiError, atomic_json, command, digest, git, now, read_json, state_root
from .state import discover


def repository_name(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:https?://|ssh://(?:[^/@]+@)?)", "", value)
    value = re.sub(r"^[^/@]+@([^:]+):", r"\1/", value).rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if (len(parts) == 3 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", parts[0])
            and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) for p in parts[1:])):
        return value.lower()
    return None


def github_json(repository, endpoint, cwd, *, pages=False):
    args = ["gh", "api", "--hostname", repository.split("/")[0], "--method", "GET"]
    if pages:
        args += ["--paginate", "--slurp"]
    try:
        return json.loads(command([*args, endpoint], cwd=cwd).stdout)
    except ValueError as exc:
        raise ApiError("invalid_response", "GitHub returned invalid pull request data.") from exc


def normalize(pull, repository):
    if not isinstance(pull, dict):
        raise ApiError("invalid_response", "GitHub returned an invalid pull request.")
    number = pull.get("number")
    url = f"https://{repository}/pull/{number}"
    if (type(number) is not int or number < 1 or not isinstance(pull.get("html_url"), str)
            or pull["html_url"].lower() != url
            or pull.get("state") not in ("open", "closed") or not isinstance(pull.get("title"), str)):
        raise ApiError("invalid_response", "GitHub pull request identity or state is invalid.")
    return {"pr": number, "repository": repository, "url": pull["html_url"], "title": pull["title"],
            "state": "MERGED" if pull.get("merged_at") else pull["state"].upper(),
            "draft": bool(pull.get("draft")),
            "head_branch": pull["head"].get("ref") if isinstance(pull.get("head"), dict) else None}


def list_pulls(request):
    cwd = request.get("cwd", ".")
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd).stdout.strip()
    sessions = discover({"cwd": cwd})["sessions"]
    from .owner_state import discover as owner_discover, toolkit
    owner_sessions = owner_discover({"cwd": cwd})["sessions"]
    remotes = git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=cwd, check=False)
    repositories = {s["repository"] for s in sessions + owner_sessions}
    for line in remotes.stdout.splitlines():
        fields = line.split(None, 1)
        name = repository_name(fields[1]) if len(fields) == 2 else None
        if name:
            repositories.add(name)
    repositories = sorted(repositories)
    repository = request.get("repository")
    if repository:
        repository = repository_name(repository)
        if repository not in repositories:
            raise ApiError("invalid_request", "Choose a repository from this checkout's remotes or saved reviews.")
    elif len(repositories) == 1:
        repository = repositories[0]
    if not repository:
        if request.get("refresh"):
            raise ApiError("invalid_request", "Choose a repository before refreshing pull requests.", repositories=repositories)
        return {"repositories": repositories, "repository": None, "checked_at": None, "pull_requests": []}
    selected_toolkit = toolkit({"cwd": cwd, "repository": repository})["toolkit"]
    sessions = [s for s in (owner_sessions if selected_toolkit == "owner" else sessions) if s["repository"] == repository]
    cache_file = state_root() / "repositories" / (digest([common, repository]) + ".json")
    cache = read_json(cache_file, {})
    if request.get("refresh"):
        endpoint = "repos/" + repository.split("/", 1)[1] + "/pulls"
        pages = github_json(repository, endpoint + "?state=open&per_page=100", cwd, pages=True)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ApiError("invalid_response", "GitHub returned incomplete pull request pages.")
        pulls = {}
        for page in pages:
            for raw in page:
                pull = normalize(raw, repository)
                pulls[pull["pr"]] = pull
        # Keep saved closed/merged reviews accessible without listing all closed PRs.
        for number in sorted({s["pr"] for s in sessions} - pulls.keys()):
            pull = normalize(github_json(repository, f"{endpoint}/{number}", cwd), repository)
            if pull["pr"] != number:
                raise ApiError("invalid_response", "GitHub returned a different saved pull request.")
            pulls[number] = pull
        cache = {"checked_at": now(), "pull_requests": list(pulls.values())}
        atomic_json(cache_file, cache)  # A failed refresh never replaces the previous list.
    pulls = {p["pr"]: dict(p) for p in cache.get("pull_requests", [])}
    for session in sessions:
        number = session["pr"]
        pulls.setdefault(number, {"pr": number, "repository": repository,
                                  "url": session.get("url"), "title": "Saved owner session" if selected_toolkit == "owner" else "Saved review", "state": "UNKNOWN"})
    for pull in pulls.values():
        pull["sessions"] = [s for s in sessions if s["pr"] == pull["pr"]]
    return {"repositories": repositories, "repository": repository, "toolkit": selected_toolkit, "checked_at": cache.get("checked_at"),
            "pull_requests": sorted(pulls.values(), key=lambda p: (p["state"] != "OPEN", -p["pr"]))}
