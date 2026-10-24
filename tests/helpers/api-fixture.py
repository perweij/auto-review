"""Create an isolated real-Git saved review for the Emacs integration test."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_api import ApiTest

fixture = ApiTest()
fixture.setUp()
fixture.temp._finalizer.detach()  # The caller owns and removes this directory.
print(json.dumps({"root": str(fixture.root), "session": str(fixture.path), "repo": str(fixture.repo)}))
