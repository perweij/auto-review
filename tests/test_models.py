"""Discovery exercises actual subprocess pipes, with no inference or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review_api import models, settings
from pr_review_api.common import ApiError


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    def executable(self, provider, code):
        path = self.root / provider
        path.write_text(f"#!{sys.executable}\nimport json, sys, os, time, subprocess\n" + code)
        path.chmod(0o755)

    def test_codex_pagination_efforts_filtering_and_no_prompts(self):
        self.executable("codex", '''
for line in sys.stdin:
    req = json.loads(line)
    assert req['method'] in ('initialize', 'initialized', 'model/list')
    if req['method'] == 'initialized': continue
    result = {}
    if req['method'] == 'model/list':
        if 'cursor' not in req['params']:
            result = {'data': [{'model':'one','displayName':'One','supportedReasoningEfforts':[{'reasoningEffort':'low'}]},
                               {'model':'hidden','hidden':True}], 'nextCursor':'next'}
        else:
            assert req['params']['cursor'] == 'next'
            result = {'data':[{'model':'two','supportedReasoningEfforts':[{'reasoningEffort':'ultra'}]}]}
    print(json.dumps({'id':req['id'],'result':result}), flush=True)
''')
        result = models.discover({"provider": "codex"})
        self.assertEqual([m["value"] for m in result["models"]], ["one", "two"])
        self.assertEqual(result["models"][1]["efforts"], ["ultra"])

    def test_claude_initialization_filters_account_and_disabled_models(self):
        self.executable("claude", '''
req = json.loads(sys.stdin.readline())
assert req['type'] == 'control_request' and req['request']['subtype'] == 'initialize'
print(json.dumps({'type':'control_response','response':{'request_id':'models','subtype':'success','response':{
    'account':{'token':'DO_NOT_RETURN'}, 'models':[
        {'value':'fable','supportedEffortLevels':['medium','high']},
        {'value':'disabled','disabled':True,'supportedEffortLevels':['low']},
        {'value':'no-effort','supportsEffort':False,'supportedEffortLevels':['high']},
        {'value':'missing-capabilities'}]}}}), flush=True)
time.sleep(60)
''')
        result = models.discover({"provider": "claude"})
        self.assertEqual(result["models"], [{"value": "fable", "label": "fable", "efforts": ["medium", "high"]}])
        self.assertNotIn("DO_NOT_RETURN", json.dumps(result))

    def test_timeout_reaps_child_and_terminates_descendants(self):
        pid_file = self.root / "child.pid"
        self.executable("codex", f'''
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open({str(pid_file)!r}, 'w').write(str(child.pid))
time.sleep(60)
''')
        started = time.monotonic()
        with self.assertRaises(ApiError) as caught:
            with models.connection("codex", timeout=0.2) as (_, receive):
                receive()
        self.assertEqual(caught.exception.code, "discovery_timeout")
        self.assertLess(time.monotonic() - started, 3)
        pid = int(pid_file.read_text())
        stat = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 1
        while stat.exists() and not stat.read_text().split(") ", 1)[1].startswith("Z") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z"))

    def test_cancel_request_cleans_discovery_child(self):
        pid_file = self.root / "provider.pid"
        self.executable("claude", f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
        cli = Path(__file__).resolve().parents[1] / "bin/pr-review"
        child = subprocess.Popen([cli, "api"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps({"protocol": 1, "command": "models", "provider": "claude"}) + "\n")
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child.terminate()
            stdout, _ = child.communicate(timeout=4)
            self.assertIn("discovery_cancelled", stdout)
            self.assertFalse(Path(f"/proc/{pid_file.read_text()}").exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdin.close()
            child.stdout.close()

    def test_selection_rejects_cross_provider_and_unsupported_effort(self):
        config = {**settings.DEFAULTS, "reviewers": ["codex"]}
        catalog = {"codex": {"provider": "codex", "models": [
            {"value": "gpt-6-astra", "efforts": ["high"]}]}}
        models.validate_selection(config, catalog)
        for field, value in (("codex_effort", "medium"), ("codex_model", "fable")):
            with self.assertRaises(ApiError):
                models.validate_selection({**config, field: value}, catalog)
        with self.assertRaises(ApiError):
            models.validate_selection(config, {"codex": {"provider": "claude", "models": []}})


if __name__ == "__main__":
    unittest.main()
