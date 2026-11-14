#!/usr/bin/env python3
"""GitLab transport fixture; rejects every non-GET request."""
import json
import os
from pathlib import Path
import subprocess
import sys
root = Path(os.environ['FIXTURE'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(tool='glab', args=args)) + '\n')
if '--version' in args:
    print('glab test'); sys.exit()
if '--help' in args:
    print('--hostname --method'); sys.exit()
assert args[:6] == ['api', '--hostname', 'gitlab.com', '--method', 'GET', args[-1]], args
endpoint = args[-1].split('?')[0]
head = subprocess.check_output(['git', '--git-dir', str(root/'remote.git'), 'rev-parse', 'refs/merge-requests/7/head'], text=True).strip()
base = subprocess.check_output(['git', '--git-dir', str(root/'remote.git'), 'rev-parse', 'refs/heads/main'], text=True).strip()
if endpoint == 'user':
    if os.environ.get('MOCK_GLAB_AUTH_FAIL'):
        sys.exit(1)
    value = dict(id=123, username='reviewer')
elif endpoint == 'projects/2':
    value = dict(id=2, path_with_namespace='fork/Sub/repo')
elif endpoint.endswith('/discussions'):
    if os.environ.get('MOCK_GLAB_DISCUSSION_FAIL'):
        sys.exit(1)
    value = []
elif endpoint.endswith('/pipelines'):
    value = [dict(id=9, sha=head, status='success', web_url='https://gitlab.com/group/Sub/repo/-/pipelines/9')]
elif endpoint == 'projects/group%2Fsub%2Frepo/merge_requests/7':
    value = dict(iid=7, web_url='https://gitlab.com/group/Sub/repo/-/merge_requests/7', title='Pilot', description='Test', author={'username':'author'}, state='opened', draft=False,
                 source_project_id=2, target_project_id=1, source_branch='feature', target_branch='main', sha=head,
                 diff_refs=dict(head_sha=head, start_sha=base, base_sha=base))
else:
    raise AssertionError(endpoint)
print(json.dumps(value))
