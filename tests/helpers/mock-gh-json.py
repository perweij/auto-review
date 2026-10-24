"""Realistic review-array/reply API fixture; never contacts GitHub."""
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FIXTURE"])
endpoint = sys.argv[1]
payload = json.load(sys.stdin)


def read(name, default):
    file = root / name
    return json.loads(file.read_text()) if file.exists() else default


def write(name, value):
    (root / name).write_text(json.dumps(value))


def count(name):
    value = int((root / name).read_text()) + 1 if (root / name).exists() else 1
    (root / name).write_text(str(value))
    return value


user = {"login": os.environ.get("MOCK_VIEWER", "reviewer")}
common = {"user": user, "created_at": "2026-01-03", "updated_at": "2026-01-03"}
if endpoint.endswith("/replies"):
    number = count("reply-count")
    if os.environ.get("MOCK_REPLY_FAIL_AT") == str(number):
        sys.exit(1)
    identity = 1000 + number
    result = {**common, "id": identity, "in_reply_to_id": int(endpoint.split("/")[-2]),
              "body": payload["body"], "pull_request_review_id": 2000 + number,
              "html_url": f"https://github.com/org/repo/pull/7#discussion_r{identity}"}
    with (root / "published-replies.jsonl").open("a") as f:
        f.write(json.dumps(result) + "\n")
    reviews = read("api-reviews.json", [])
    reviews.append({"id": 2000 + number, "body": "", "state": "COMMENTED", "user": user,
                    "html_url": f"https://github.com/org/repo/pull/7#pullrequestreview-{2000+number}",
                    "submitted_at": "2026-01-03"})
    write("api-reviews.json", reviews)
else:
    number = count("post-count")
    if os.environ.get("MOCK_POST") == "reject":
        sys.exit(1)
    identity = 3000 + number
    result = {"id": identity, "body": payload["body"], "commit_id": payload["commit_id"], "user": user,
              "state": {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[payload["event"]],
              "submitted_at": "2026-01-03", "html_url": f"https://github.com/org/repo/pull/7#pullrequestreview-{identity}"}
    reviews = read("api-reviews.json", [])
    reviews.append(result)
    write("api-reviews.json", reviews)
    inline, threads = read("api-inline.json", []), read("api-threads.json", [])
    for index, comment in enumerate(payload.get("comments", [])):
        cid = 4000 + number * 100 + index
        value = {**common, **comment, "id": cid, "pull_request_review_id": identity,
                 "html_url": f"https://github.com/org/repo/pull/7#discussion_r{cid}",
                 "original_line": comment["line"], "commit_id": payload["commit_id"]}
        inline.append(value)
        threads.append({"id": f"T{cid}", "isResolved": False, "isOutdated": False, "viewerCanReply": True,
                        "path": comment["path"], "line": comment["line"], "originalLine": comment["line"],
                        "comments": {"totalCount": 1, "nodes": [{"databaseId": cid}]}})
    write("api-inline.json", inline)
    write("api-threads.json", threads)
    write("api-payload.json", payload)
print(json.dumps(result))
if os.environ.get("MOCK_POST") == "ambiguous":
    sys.exit(1)
