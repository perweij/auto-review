"""Explicitly scoped, once-only forge fault configuration."""
from contextlib import contextmanager
import json
import os
import shutil
from .runner import atomic_json, Failure


@contextmanager
def fault(run, name, **spec):
    config = run.directory / (name + '-fault.json')
    trace = run.directory / 'trace-bin/gh'
    atomic_json(config, dict(executable=str(trace) if trace.exists() else shutil.which('gh'),
                            method='POST', mode='after', **spec))
    wrapper = run.directory / 'fault-bin'
    wrapper.mkdir(exist_ok=True)
    if not (wrapper / 'gh').exists():
        (wrapper / 'gh').symlink_to(run.root / 'tests/forge_smoke/fault.py')
    old_path = run.env['PATH']
    run.env.update(PATH=str(wrapper)+os.pathsep+old_path, SMOKE_FAULT_FILE=str(config))
    try:
        yield config
    finally:
        run.env['PATH'] = old_path
        run.env.pop('SMOKE_FAULT_FILE', None)


def accepted(config, *, readback=False):
    evidence = json.loads(config.read_text())
    if not evidence.get('used') or evidence.get('forwarded_exit') != 0 or not evidence.get('response_bytes'):
        raise Failure('Fault did not hide an accepted remote write: ' + str(config))
    if readback and not evidence.get('readback_failed'):
        raise Failure('Immediate readback was not interrupted')
