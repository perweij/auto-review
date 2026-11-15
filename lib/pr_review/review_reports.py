"""Execution report identities independent of movable review-generation paths."""
from pathlib import Path
import re
import uuid

from .common import ApiError, digest, read_json
from . import session_trace, session_report

STORE = 'review-execution-reports'


def new_generation(manifest):
    """Call only at a durable generation boundary, never during a read/resume."""
    previous = manifest.get('execution_report_id')
    manifest['execution_report_id'] = uuid.uuid4().hex
    manifest['execution_trace_version'] = 2
    manifest.pop('execution_report', None)
    manifest.pop('report_warning', None)
    if previous:
        manifest['execution_report_parent'] = previous
    else:
        manifest.pop('execution_report_parent', None)


def location(root, manifest):
    identity = manifest.get('execution_report_id')
    if (manifest.get('execution_trace_version') != 2 or not isinstance(identity, str)
            or not re.fullmatch('[0-9a-f]{32}', identity)):
        raise ApiError('report_unavailable', 'Execution reports require a new traced review generation.')
    return Path(root) / STORE / identity


def binding(root, manifest):
    location(root, manifest)  # Validate before trusting the saved identity.
    return digest([str(Path(root).resolve()), manifest['execution_report_id']])


def generate(session):
    """Render only the explicitly selected generation; never reconstruct a trace."""
    folder = location(session.root, session.manifest)
    trace = session_trace.validate(read_json(folder / 'execution-trace.json'), binding(session.root, session.manifest))
    calls = read_json(session.root / 'usage/ledger.json', {}).get('calls', {})
    data = session_report.report_data(trace, calls, outcome=session.manifest.get('status', 'unknown'), shared_ledger=True)
    data['review'] = dict(generation=session.id, url=session.manifest.get('url'),
                          snapshot=session.manifest.get('snapshot', {}),
                          scope=session.manifest.get('analysis_kind', 'full'))
    fields, body = session_report.write_report(folder / 'reports', data)
    fields.update(generation=session.id, report_coverage=data['coverage'],
                  report_snapshot=digest([binding(session.root, session.manifest), trace]))
    previous = session.manifest.get('execution_report', {})
    if previous.get('report_snapshot') == fields['report_snapshot']:
        for key in ('report_publication', 'report_url'):
            if key in previous:
                fields[key] = previous[key]
    return fields, body


def dispatch(request):
    from .state import Session
    from .common import locked, atomic_json
    from .repository import Repository
    from .forge import check_account, require, restore
    from . import report_publication
    session = Session(request)
    publishing = request['command'] == 'review-publish-report'
    if publishing:
        require(restore(session.manifest).forge, 'publication')
    with locked(session.pr_lock):
        session = Session(request)
        with locked(session.lock):
            if publishing and request.get('revision') is not None:
                if request['revision'] != digest(session.load()):
                    raise ApiError('stale_revision', 'Review changed; reload before publishing its execution report.')
            fields, body = generate(session)
            if not publishing:
                session.manifest['execution_report'] = fields
                atomic_json(session.path / 'manifest.json', session.manifest)
                return fields
            repo = Repository.from_manifest(session.manifest)
            account = repo.forge.account(repo.project, repo.cwd)
            check_account(session.manifest.get('forge_account'), account, session.manifest.get('viewer'))
            folder = location(session.root, session.manifest)
            journal = session_trace.validate(read_json(folder / 'execution-trace.json'), binding(session.root, session.manifest))
            key = digest([binding(session.root, session.manifest), journal])
            path = folder / 'publication.json'
            actions = read_json(path, {})
            fields.update(report_publication.publish(session, actions, key, body,
                session.manifest['viewer'], lambda: atomic_json(path, actions)))
            session.manifest['execution_report'] = fields
            atomic_json(session.path / 'manifest.json', session.manifest)
            return fields


def cli(argv, command):
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(prog='pr-review ' + command)
    parser.add_argument('session')
    parser.add_argument('--generation')
    args = vars(parser.parse_args(argv))
    args['command'] = 'review-' + command
    try:
        print(json.dumps(dispatch(args), indent=2))
        return 0
    except (ApiError, OSError, ValueError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1


class Recording:
    """Split measured execution at explicit generation boundaries."""
    def __init__(self, run):
        self.run, self.scope, self.finished = run, None, []

    def start(self):
        if self.run.manifest.get('execution_trace_version') != 2:
            return
        self.scope = session_trace.operation(location(self.run.root, self.run.manifest), True,
                                             binding(self.run.root, self.run.manifest))
        self.scope.__enter__()

    def stop(self, error=None):
        if self.scope is None:
            return
        import copy
        from .state import generation
        from types import SimpleNamespace
        outcome = self.run.manifest.get('status', 'unknown')
        session_trace.decision('boundary/' + outcome,
            sources=[session_trace.marked('review_result') or session_trace.marked('acceptance_result')])
        self.scope.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
        self.scope = None
        manifest = copy.deepcopy(self.run.manifest)
        session_trace.boundary(location(self.run.root, manifest), binding(self.run.root, manifest),
                               {'outcome': 'needs_attention' if outcome == 'paused' else outcome})
        self.finished.append(SimpleNamespace(root=self.run.root, manifest=manifest,
                             id=generation(self.run.path, manifest)))

    def __enter__(self):
        self.run.report_recording = self
        self.start()
        return self

    def __exit__(self, kind, error, traceback):
        self.stop(error)
        self.run.report_recording = None

    def render(self):
        from .events import emit
        for session in self.finished:
            try:
                from .state import Session, generation
                selected = Session({'session': str(session.root)})
                for path in selected.paths():
                    manifest = read_json(path / 'manifest.json', {})
                    if manifest.get('execution_report_id') == session.manifest['execution_report_id']:
                        session.manifest, session.id = manifest, generation(path, manifest)
                        break
                fields, _ = generate(session)
                if session.manifest['execution_report_id'] == self.run.manifest.get('execution_report_id'):
                    self.run.manifest['execution_report'] = fields
                    self.run.save()
                emit('progress', message=f"Execution report: {fields['report_path']} (generation {session.id})")
            except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
                self.run.manifest['report_warning'] = str(exc)
                try:
                    self.run.save()
                except (ApiError, OSError):
                    pass
                emit('progress', message='Execution report unavailable: ' + str(exc))


def allocate(run):
    recording = getattr(run, 'report_recording', None)
    if recording:
        recording.stop()
    previous = run.manifest.get('execution_report_id')
    saved_sources = run.manifest.get('execution_report_sources', {})
    sources = dict(saved_sources) if isinstance(saved_sources, dict) else {}
    if previous:
        try:
            saved = session_trace.validate(read_json(location(run.root, run.manifest) / 'execution-trace.json'), binding(run.root, run.manifest))
        except (ApiError, OSError, ValueError, TypeError):
            saved = {}
        for stage in saved.get('stages', []):
            artifact = stage.get('artifact')
            if artifact and stage.get('status') in ('completed', 'reused'):
                try:
                    relative = str(Path(artifact).relative_to(run.path))
                except ValueError:
                    continue
                sources[stage['role'] + ':' + relative] = dict(report=previous, stage=stage['id'],
                    artifact_hash=stage.get('artifact_hash'), choice=stage['choice'])
    new_generation(run.manifest)
    run.manifest['execution_report_sources'] = sources
    session_trace.initialize(location(run.root, run.manifest), binding(run.root, run.manifest))
    if recording:
        recording.start()


def reuse_source(run, role, artifact):
    from .storage import hash_file
    try:
        key = role + ':' + str(Path(artifact).relative_to(run.path))
        source = run.manifest.get('execution_report_sources', {}).get(key)
        if (not isinstance(source, dict) or not isinstance(source.get('choice'), dict)
                or not isinstance(source.get('report'), str) or not isinstance(source.get('stage'), str)):
            return None
        return source if source.get('artifact_hash') == hash_file(Path(artifact)) else None
    except (ValueError, OSError, AttributeError, TypeError):
        return None
