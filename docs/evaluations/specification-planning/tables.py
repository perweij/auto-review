#!/usr/bin/env python3
"""Render the paired result tables used in README.md from the saved result directories.

    python3 tables.py results/round1 results/round2 results/control-retry
"""
import json
from pathlib import Path
import sys


def load(directory):
    runs = {}
    for path in sorted((Path(directory) / 'runs').glob('*/result.json')):
        record = json.loads(path.read_text())
        runs[record['id']] = record
    return runs


def usage(record):
    u = record['implementation']['usage']
    r = record['implementation'].get('review_usage', {})
    p = record['planning']['usage']
    if u.get('status') == 'unavailable':
        return 'unavailable'
    f = lambda x: f"{x['input_tokens']:,}" if x.get('status') in ('reported', 'partial') else 'n/a'
    return f"{f(p)} / {f(u)} / {f(r)}"


def row(record):
    i, p, s = record['implementation'], record['planning'], record['score']
    return (f"{record['status']} ({i.get('stop')}) | {s['acceptance_passed']}/{s['acceptance_total']} | {i.get('repair_rounds', 0)} | "
            f"{p['elapsed_seconds']:.0f} / {i.get('elapsed_seconds', 0):.0f} | {usage(record)}")


def main(first, second, retry=None):
    control, strengthened, retried = load(first), load(second), load(retry) if retry else {}
    print('| Pair | Arm | Outcome (stop) | Acceptance | Repairs | Plan / impl+review s | Input tokens plan / impl / review |')
    print('| --- | --- | --- | ---: | ---: | ---: | --- |')
    for pair in dict.fromkeys(k.rsplit('.', 1)[0] for k in control):
        for label, source, arm in (('control (round 1)', control, 'control'), ('control retry', retried, 'control'),
                                   ('strengthened round 1', control, 'strengthened'), ('strengthened round 2', strengthened, 'strengthened')):
            record = source.get(f'{pair}.{arm}')
            if record:
                print(f"| {pair.replace('.', '/')} | {label} | {row(record)} |")


if __name__ == '__main__':
    main(*sys.argv[1:])
