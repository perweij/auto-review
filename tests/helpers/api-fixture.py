"""Create an isolated real-Git saved review for the Emacs integration test."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_api import ApiTest
from pr_review.common import atomic_json

fixture = ApiTest()
fixture.setUp()
if "--conversation" in sys.argv:
    thread = {"key": "thread:T1", "kind": "thread", "root_id": 101,
              "path": "source.txt", "line": 2, "original_line": 2,
              "is_resolved": False, "is_outdated": False, "can_reply": True,
              "comments": [{"id": 101, "author": "reviewer", "body": "Published comment"},
                           {"id": 102, "author": "reviewer", "body": "Reply added on GitHub"}]}
    atomic_json(fixture.path / "discussion/snapshot-a/discussion.json", {"items": [thread]})
    state = fixture.session.load()
    state["comments"][0].update(status="published", receipt={"id": 101})
    fixture.session.save(state)
fixture.temp._finalizer.detach()  # The caller owns and removes this directory.
result = {"root": str(fixture.root), "session": str(fixture.path), "repo": str(fixture.repo)}
if "--run-result" in sys.argv:
    from pr_review.application import dispatch
    # Exercise the production completion response, bypassing only generation.
    with patch("pr_review.reviews.execute", return_value=fixture.session):
        result["run_result"] = dispatch({"command": "run", "cwd": str(fixture.repo),
                                         "pr": 7, "repository": "github.com/org/repo"})
print(json.dumps(result))
