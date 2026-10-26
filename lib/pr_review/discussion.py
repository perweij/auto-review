"""Complete GitHub conversations, with legacy-compatible freshness identities."""
import copy
import tempfile
from pathlib import Path

from .common import ApiError, ROOT, now, read_json
from .events import emit
from .repository import Repository
from .storage import write_json, hash_file


def normalize(issues, reviews, inline, threads):
    def invalid():
        raise ApiError("incomplete_discussion", "Incomplete or inconsistent discussion snapshot")

    def valid_comment(c):
        return (isinstance(c, dict) and type(c.get("id")) is int and c["id"] > 0
                and isinstance(c.get("user"), dict)
                and isinstance(c["user"].get("login"), str)
                and all(isinstance(c.get(k), str) for k in ("body", "html_url", "created_at", "updated_at")))

    def comment(c):
        return {"id": c["id"], "author": c["user"]["login"], "body": c["body"],
                "url": c["html_url"], "created_at": c["created_at"], "updated_at": c["updated_at"]}

    if not all(isinstance(v, list) for v in (issues, reviews, inline, threads)):
        invalid()
    if not all(valid_comment(c) for c in issues + inline):
        invalid()
    try:
        if any(type(r.get("id")) is not int or not isinstance(r["user"]["login"], str)
               or any(not isinstance(r.get(k), str) for k in ("body", "html_url", "state"))
               or (r["state"] != "PENDING" and not isinstance(r.get("submitted_at"), str)) for r in reviews):
            invalid()
        for thread in threads:
            if (not isinstance(thread.get("id"), str)
                    or any(type(thread.get(k)) is not bool for k in ("isResolved", "isOutdated", "viewerCanReply"))
                    or len(thread["comments"]["nodes"]) != 1
                    or type(thread["comments"]["nodes"][0]["databaseId"]) is not int
                    or type(thread["comments"]["totalCount"]) is not int):
                invalid()
        roots = [t["comments"]["nodes"][0]["databaseId"] for t in threads]
        if (len(roots) != len(set(roots))
                or sorted(c["id"] for c in inline if c.get("in_reply_to_id") is None) != sorted(roots)
                or any((c.get("in_reply_to_id") or c["id"]) not in roots for c in inline)):
            invalid()
        items = []
        for thread, root in zip(threads, roots):
            comments = sorted((c for c in inline if (c.get("in_reply_to_id") or c["id"]) == root),
                              key=lambda c: (c["created_at"], c["id"]))
            if len(comments) != thread["comments"]["totalCount"]:
                invalid()
            items.append({"key": "thread:" + thread["id"], "kind": "thread", "root_id": root,
                          "url": next(c["html_url"] for c in comments if c["id"] == root),
                          "path": thread.get("path"), "line": thread.get("line"),
                          "original_line": thread.get("originalLine"), "is_resolved": thread["isResolved"],
                          "is_outdated": thread["isOutdated"], "can_reply": thread["viewerCanReply"],
                          "can_resolve": thread.get("viewerCanResolve"), "can_unresolve": thread.get("viewerCanUnresolve"),
                          "resolved_by": (thread.get("resolvedBy") or {}).get("login"),
                          "comments": [comment(c) for c in comments]})
        items += [{"key": "comment:" + str(c["id"]), "kind": "comment", "comments": [comment(c)]} for c in issues]
        items += [{"key": "review:" + str(r["id"]), "kind": "review", "state": r["state"],
                   "comments": [{"id": r["id"], "author": r["user"]["login"], "body": r["body"],
                                 "url": r["html_url"], "created_at": r["submitted_at"], "updated_at": r["submitted_at"]}]}
                  for r in reviews if r["state"] != "PENDING"]
        return {"format_version": 1, "items": sorted(items, key=lambda item: item["key"])}
    except (TypeError, KeyError, AttributeError, StopIteration):
        invalid()


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
    """Fetch complete discussion without depending on a reviewer session."""
    values = {}
    for name, endpoint in (("issues", f"issues/{repository.pr}/comments"),
                           ("reviews", f"pulls/{repository.pr}/reviews"),
                           ("inline", f"pulls/{repository.pr}/comments")):
        values[name] = repository.pages(f"repos/{repository.name}/{endpoint}")
    threads, cursor, seen = [], None, set()
    while True:
        variables = dict(owner=repository.name.split("/")[0], name=repository.name.split("/")[1], number=repository.pr)
        if cursor:
            variables["cursor"] = cursor
        try:
            data = repository.graphql((ROOT / "prompts/threads.graphql").read_text(), **variables)
            pr = data["repository"]["pullRequest"]
            page = pr["reviewThreads"]
            if pr["number"] != repository.pr or not isinstance(page["nodes"], list) or type(page["pageInfo"]["hasNextPage"]) is not bool:
                raise KeyError("thread page")
            threads.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"].get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise KeyError("thread cursor")
            seen.add(cursor)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ApiError("incomplete_discussion", "Incomplete GraphQL thread response") from exc
    values["threads"] = threads
    return normalize(values["issues"], values["reviews"], values["inline"], threads), values


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
    reviews, cursor, seen = [], None, set()
    try:
        while True:
            variables = dict(owner=repository.name.split("/")[0], name=repository.name.split("/")[1], number=repository.pr)
            if cursor:
                variables["cursor"] = cursor
            data = repository.graphql((ROOT / "prompts/review-status.graphql").read_text(), **variables)
            pr = data["repository"]["pullRequest"]
            page = pr["latestOpinionatedReviews"]
            if (pr["number"] != repository.pr or not isinstance(data["viewer"]["login"], str)
                    or not isinstance(page["nodes"], list) or type(page["pageInfo"]["hasNextPage"]) is not bool
                    or any(not isinstance(r["author"]["login"], str) or not isinstance(r["state"], str) for r in page["nodes"])):
                return
            reviews.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"].get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return
            seen.add(cursor)
        write_json(destination, {"available": True, "checked_at": now(), "decision": pr.get("reviewDecision"),
                                 "latest_activity": pr.get("viewerLatestReview"),
                                 "opinionated": next((r for r in reviews if r["author"]["login"].lower() == data["viewer"]["login"].lower()), None)})
    except (ApiError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ApiError) and exc.code == "cancelled":
            raise
        return  # Review status is optional; it must never imply approval.


def refresh(session):
    repository = Repository.from_manifest(session.manifest)
    repository.metadata(session.path / "publication/pr-latest.json")
    collect(repository, session.path)
    collect_status(repository, session.path)
