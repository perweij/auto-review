#!/usr/bin/env python3
"""Copy the reviewable parts of a live run directory into results/ (no raw event logs).

    python3 export_results.py /tmp/spec-eval results/round1

Kept: planning records, specifications, planner prompts/schemas/responses, implementation
and review prompts, per-run result.json and the candidate diff against the baseline.
Raw provider event logs and scratch space stay in the local run directory.
"""
from pathlib import Path
import shutil
import subprocess
import sys


def scrub(root, replacements):
    for path in Path(root).rglob('*'):
        if path.is_file():
            text = path.read_text(errors='replace')
            for old, new in replacements:
                text = text.replace(old, new)
            path.write_text(text)


def main(source, target):
    source, target = Path(source), Path(target)
    for path in (source / 'planning').rglob('*'):
        if path.is_file() and (path.name in ('planning.json', 'specification.md') or (path.name in ('prompt.txt', 'schema.json', 'result.json') and 'turn-' in str(path))):
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    for run in sorted((source / 'runs').glob('*/result.json')):
        destination = target / 'runs' / run.parent.name
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run, destination / 'result.json')
        work = source / 'work' / run.parent.name
        for prompt in work.glob('*/prompt.txt'):
            if prompt.parent.name.startswith(('turn-', 'review-')):
                (destination / prompt.parent.name).mkdir(exist_ok=True)
                shutil.copyfile(prompt, destination / prompt.parent.name / 'prompt.txt')
        if (work / 'spec' / 'revision-1.md').exists():
            shutil.copyfile(work / 'spec' / 'revision-1.md', destination / 'specification.md')
        repo = work / 'repo'
        if repo.exists():   # a git failure raises CalledProcessError with stderr: never write a silently empty diff
            exclude = ':(exclude,glob)**/__pycache__/**'
            subprocess.run(['git', 'add', '-A', '-N', '--', '.', exclude], cwd=repo, text=True, capture_output=True, check=True)
            diff = subprocess.run(['git', 'diff', 'HEAD', '--', '.', exclude], cwd=repo, text=True, capture_output=True, check=True).stdout
            (destination / 'changes.diff').write_text(diff)
    scrub(target, [(str(source.resolve()), '<OUT>'), (str(Path.home()), '<HOME>')])


if __name__ == '__main__':
    main(*sys.argv[1:])
