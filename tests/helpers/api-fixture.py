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
if any(arg.startswith('--automatic') for arg in sys.argv):
    from pr_review.automation import AutomaticRun
    from pr_review.state import Session
    from pr_review.common import state_root
    fixture.manifest['draft_mutations_tracked'] = True
    if '--automatic-paused' in sys.argv:
        fixture.manifest['status'] = 'paused'
        (fixture.path / 'publication/contributions.json').unlink()
    atomic_json(fixture.path / 'manifest.json', fixture.manifest)
    fixture.session = Session({'session': str(fixture.path)})
    if '--automatic-paused' not in sys.argv:
        state = fixture.session.load()
        state['comments'][0]['classification'] = 'UNCERTAIN'
        state['comments'][1]['placement'] = {'kind': 'unplaced'}
        fixture.session.save(state)
    operation_id = 'a' * 32
    log = state_root() / 'operations' / (operation_id + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('Saved automatic analysis progress\n')
    run = AutomaticRun({'operation_id': operation_id, 'log_path': str(log)})
    run.bind(fixture.session)
    run.finish('needs_attention', 'Analysis paused.' if '--automatic-paused' in sys.argv
               else 'Automatic publication stopped: 2 findings need attention. No comments were published. Analysis is saved.')
fixture.temp._finalizer.detach()  # The caller owns and removes this directory.
result = {"root": str(fixture.root), "session": str(fixture.path), "repo": str(fixture.repo)}
if "--run-result" in sys.argv:
    from pr_review.application import dispatch
    # Exercise the production completion response, bypassing only generation.
    with patch("pr_review.reviews.execute", return_value=fixture.session):
        result["run_result"] = dispatch({"command": "run", "cwd": str(fixture.repo),
                                         "pr": 7, "repository": "github.com/org/repo"})
print(json.dumps(result))
