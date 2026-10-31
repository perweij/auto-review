#!/usr/bin/env python3
"""Six live Luna/medium calls, alternating arm order; consumes model quota."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    root = Path(tempfile.mkdtemp(prefix='auto-review-map-comparison-'))
    print('Artifacts:', root, flush=True)
    for case, arms in [('A', ['control', 'map']), ('B', ['map', 'control']),
                       ('C', ['control', 'map'])]:
        for arm in arms:
            out = root / (case + '-' + arm)
            args = [sys.executable, str(HERE/'run.py'), '--case', case, '--directory', str(out)]
            if arm == 'map':
                args.append('--repo-map')
            subprocess.run(args, check=True)
            records = json.loads((out/'results.json').read_text())
            entry = records[0]
            if case != 'A':
                checkout = out/entry['case']/'repo'
                env = {**os.environ, 'PYTHONPATH': 'lib:tests', 'PYTHONDONTWRITEBYTECODE': '1'}
                with (checkout.parent/'regression-module.log').open('w') as log:
                    try:
                        result = subprocess.run(
                            [sys.executable, '-m', 'unittest', 'test_suggestions' if case == 'B' else 'test_owner'],
                            cwd=checkout, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=90)
                        code = result.returncode
                    except subprocess.TimeoutExpired:
                        code = 124
                entry['regression_module_exit_code'] = code
                (out/'results.json').write_text(json.dumps(records, indent=2)+'\n')
    print('Complete:', root, flush=True)


if __name__ == '__main__':
    main()
