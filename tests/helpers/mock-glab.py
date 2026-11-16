#!/usr/bin/env python3
"""GitLab transport fixture; report notes require explicit test opt-in."""
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
    print('--hostname --method --input'); sys.exit()
assert args[:4] == ['api', '--hostname', 'gitlab.com', '--method'], args
method = args[4]
assert method == 'GET' or os.environ.get('MOCK_GLAB_REPORTS') == '1' or os.environ.get('MOCK_GLAB_WRITES') == '1', args
endpoint = args[5].split('?')[0]
notes = root / 'gitlab-notes.json'
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
    value = [dict(id='opaque-' + str(n['id']), individual_note=not bool(n.get('position')), notes=[n])
             for n in json.loads(notes.read_text())] if notes.exists() else []
    if method == 'POST':
        payload = json.load(sys.stdin)
        rows = json.loads(notes.read_text()) if notes.exists() else []
        note = dict(id=len(rows) + 1, body=payload['body'], author={'id': 123, 'username': 'reviewer'},
                    system=False, project_id=1, noteable_type='MergeRequest', noteable_id=70, noteable_iid=7,
                    created_at='2026-11-16T00:00:00Z', updated_at='2026-11-16T00:00:00Z',
                    resolvable=True, resolved=False, position=payload['position'])
        rows.append(note)
        notes.write_text(json.dumps(rows))
        value = dict(id='opaque-' + str(note['id']), individual_note=False, notes=[note])
elif endpoint.endswith('/pipelines'):
    value = [dict(id=9, sha=head, status='success', web_url='https://gitlab.com/group/Sub/repo/-/pipelines/9')]
elif endpoint == 'projects/1/merge_requests/7/notes':
    value = json.loads(notes.read_text()) if notes.exists() else []
    if method == 'POST':
        payload = json.load(sys.stdin)
        note = dict(id=len(value) + 1, body=payload['body'], author={'id': 123, 'username': 'reviewer'},
                    system=False, project_id=1, noteable_type='MergeRequest', noteable_id=70, noteable_iid=7,
                    created_at='2026-11-16T00:00:00Z', updated_at='2026-11-16T00:00:00Z', resolvable=False)
        value.append(note)
        notes.write_text(json.dumps(value))
        value = note
elif '/versions' in endpoint:
    version = dict(id=4, state='collected', head_commit_sha=head, base_commit_sha=base, start_commit_sha=base)
    value = [version] if endpoint.endswith('/versions') else version
elif endpoint == 'projects/group%2Fsub%2Frepo/merge_requests/7':
    value = dict(id=70, iid=7, web_url='https://gitlab.com/group/Sub/repo/-/merge_requests/7', title='Pilot', description='Test', author={'username':'author'}, state='opened', draft=False,
                 source_project_id=2, target_project_id=1, source_branch='feature', target_branch='main', sha=head,
                 diff_refs=dict(head_sha=head, start_sha=base, base_sha=base))
else:
    raise AssertionError(endpoint)
print(json.dumps(value))
