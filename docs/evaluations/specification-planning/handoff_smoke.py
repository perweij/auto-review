#!/usr/bin/env python3
"""Live check that each implementation provider can read the handed Markdown file.

The specification (outside the workspace) asks for a file whose content is a nonce that
appears only in that specification. Consumes a few provider calls; never part of the
deterministic suite.   python3 handoff_smoke.py OUT_DIR
"""
import json
from pathlib import Path
import sys
import uuid

import evaluate as e
from pr_review import issue_harness, specification
from pr_review.owner_providers import context_prompt

PROVIDERS = {'codex': {'provider': 'codex', 'model': 'gpt-6.1-terra', 'effort': 'low'},
             'claude': {'provider': 'claude', 'model': 'haiku', 'effort': 'low'}}


def main(out):
    out = Path(out)
    manifest = e.load_manifest()
    results = {}
    for name, choice in PROVIDERS.items():
        directory = out / name
        checkout = directory / 'repo'
        baseline = e.baseline_checkout(manifest, checkout)
        nonce = 'NONCE-' + e.sha256(name.encode())[:12]
        document = specification.render(f'## Outcome\nCreate the file HANDOFF.txt in the repository root containing exactly `{nonce}` and a newline. '
                                        'Ünïcode check: ✓ 日本語.\n\n## Scope\nOnly that file.\n\n## Approach\nNo source changes.\n\n'
                                        '## Acceptance scenarios\n- HANDOFF.txt exists.\n\n## Verification\nRead the file back.', ['HANDOFF.txt holds the nonce'])
        path = directory / 'spec' / 'revision-1.md'
        stored = specification.write(path, document)
        handle = {'format': 'markdown', 'file': str(path), 'revision': 1, 'sha256': stored['sha256'], 'bytes': stored['bytes']}
        context = e.implementation_context(manifest, {'body': 'Follow the specification file.', 'title': 'Handoff check'},
                                           {'comments': []}, handle, checkout, baseline, directory, [])
        prompt, _ = context_prompt((e.ROOT / 'prompts' / 'issue-implement.txt').read_text() + '\n', context, directory)
        call = e.provider_call(choice, prompt, directory / 'turn-1', checkout, issue_harness.SCHEMA, {'session_id': str(uuid.uuid4()) if choice['provider'] == 'claude' else None}, 600)
        target = checkout / 'HANDOFF.txt'
        results[name] = {'choice': choice, 'status': call['status'], 'error': call.get('error'), 'elapsed_seconds': call['elapsed_seconds'],
                         'usage': call['usage'], 'file_created': target.exists(),
                         'content_matches': target.exists() and target.read_text() == nonce + '\n',
                         'spec_unchanged': path.read_bytes() == document.encode('utf-8'),
                         'report': call.get('value')}
        print(name, results[name]['status'], results[name]['content_matches'], flush=True)
    (out / 'handoff-smoke.json').write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main(sys.argv[1])
