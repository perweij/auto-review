#!/usr/bin/env python3
"""Mechanical measures that support (never replace) the manual specification audit.

    python3 audit.py OUT_DIR    # prints JSON per task/arm

Measures: document size, level-two sections, backticked paths that do not exist in the
fixture and are not plausible new files, line-number references, diff hunks, checklist
items, and how many decisions the planner declared per kind/status. Whether an
assumption is unsupported, a scope addition unwarranted or a prescription brittle is a
judgement recorded in the README.
"""
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / 'lib'))
from pr_review import specification


def measure(document, fixture):
    paths = set(re.findall(r'`([\w./-]+\.(?:py|md|json|txt))`', document))
    existing = {p.relative_to(fixture).as_posix() for p in fixture.rglob('*') if p.is_file()}
    return {'characters': len(document), 'sections': [t for t, _ in specification.sections(document)],
            'paths_mentioned': sorted(paths), 'paths_not_in_fixture': sorted(p for p in paths if p not in existing),
            'line_number_references': len(re.findall(r'\bline[s]? \d+|:\d+\b', document)),
            'diff_hunks': len(re.findall(r'^@@ ', document, re.M)), 'checklist_items': len(re.findall(r'^\s*[-*] \[[ xX]\]', document, re.M))}


def main(out):
    manifest = json.loads((HERE / 'manifest.json').read_text())
    fixture = HERE / manifest['fixture']['path']
    result = {}
    for directory in sorted((Path(out) / 'planning').iterdir()):
        record = json.loads((directory / 'planning.json').read_text())
        entry = {'status': record['status'], 'clarification_turns': record['clarification_turns'],
                 'elapsed_seconds': record['elapsed_seconds']}
        document = (directory / 'specification.md')
        if document.exists():
            entry.update(measure(document.read_text(encoding='utf-8'), fixture))
        final = next((t['response'] for t in reversed(record['turns']) if t.get('response')), {})
        decisions = final.get('decisions', [])
        entry['decisions'] = {f"{d['kind']}/{d['status']}": sum(1 for x in decisions if (x['kind'], x['status']) == (d['kind'], d['status']))
                              for d in decisions}
        entry['questions_asked'] = sum(len(q) for q in record['questions'])
        result[directory.name] = entry
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main(sys.argv[1])
