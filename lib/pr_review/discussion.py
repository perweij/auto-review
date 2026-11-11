"""Complete GitHub conversations, with legacy-compatible freshness identities."""
import copy
import tempfile
from pathlib import Path

from .common import ApiError, ROOT, now, read_json
from .events import emit
from .repository import Repository
from .storage import write_json, hash_file


def normalize(issues, reviews, inline, threads):
    from .forge_github import normalize as github_normalize
    return github_normalize(issues, reviews, inline, threads)


def external(discussion, actions):
    replies, reviews = set(), set()
    for action in actions:
        if action.get("status") != "published":
            continue
        receipt = action.get("result") or {}
        if action.get("kind") == "reply":
            replies.add(receipt.get("id"))
            reviews.add(receipt.get("pull_request_review_id"))
        else:
            reviews.add(receipt.get("id"))
        replies.update(c.get("id") for c in action.get("comment_results", {}).values())
    result = copy.deepcopy(discussion)
    items = []
    for item in result["items"]:
        if item["kind"] == "thread":
            item["comments"] = [c for c in item["comments"] if c["id"] not in replies]
            if not item["comments"]:
                continue
        elif item["kind"] == "review" and item["comments"][0]["id"] in reviews:
            continue
        items.append(item)
    result["items"] = items
    return result


def fetch(repository):
    """Compatibility entry point returning normalized conversations and diagnostics."""
    from .forge_context import validate_discussion
    discussion, diagnostics = repository.forge.discussion(repository)
    return validate_discussion(discussion), diagnostics


def collect(repository, directory):
    directory = Path(directory)
    (directory / "discussion").mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=directory / "discussion"))
    manifest = read_json(directory / "manifest.json")
    try:
        discussion, values = fetch(repository)
        for name, value in values.items():
            write_json(snapshot / (name + ".json"), value)
        write_json(snapshot / "discussion.json", discussion)
        from .receipts import actions as saved_actions
        actions = saved_actions(directory)
        write_json(snapshot / "external.json", external(discussion, actions), sort_keys=True)
        manifest["discussion"] = {"complete": True, "hash": hash_file(snapshot / "external.json"),
                                  "path": str(snapshot / "discussion.json"), "fetched_at": now()}
        write_json(directory / "manifest.json", manifest)
        return discussion
    except (ApiError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ApiError) and exc.code == "cancelled":
            raise
        message = str(exc) if isinstance(exc, ApiError) else "Incomplete GraphQL thread response"
        manifest.setdefault("discussion", {}).update(complete=False, error=message)
        manifest.setdefault("publication", {})["status"] = "paused"
        write_json(directory / "manifest.json", manifest)
        emit("progress", message=f"Warning: {message}. Publication paused; local reports and drafts are retained.")
        raise ApiError("incomplete_discussion", message) from exc


def collect_status(repository, directory):
    destination = Path(directory) / "discussion/review-status.json"
    write_json(destination, {"available": False})
    value = repository.forge.review_status(repository)
    write_json(destination, value or {"available": False})


def refresh(session):
    repository = Repository.from_manifest(session.manifest)
    repository.metadata(session.path / "publication/pr-latest.json")
    collect(repository, session.path)
    collect_status(repository, session.path)
