"""Metadata-only transport used by unattended CLI integration fixtures."""
import json
import os
import sys

if os.environ.get('MOCK_DISCOVERY_ERROR'):
    sys.exit(1)
for line in sys.stdin:
    request = json.loads(line)
    efforts = ['low', 'medium', 'high', 'xhigh', 'max']
    if sys.argv[1] == 'codex':
        if request['method'] == 'initialized':
            continue
        result = {} if request['method'] == 'initialize' else {
            'data': [{'model': name, 'supportedReasoningEfforts': [{'reasoningEffort': e} for e in efforts]}
                     for name in ('gpt-6-astra', 'gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.1-terra')], 'nextCursor': None}
        response = {'id': request['id'], 'result': result}
    else:
        response = {'type': 'control_response', 'response': {'request_id': request['request_id'],
                    'subtype': 'success', 'response': {'models': [
                        {'value': name, 'supportedEffortLevels': efforts} for name in ('fable', 'haiku', 'opus')]}}}
    print(json.dumps(response), flush=True)
