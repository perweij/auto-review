"""Read-only user-facing checkout discovery and progress announcements."""
from pathlib import Path

from .common import ApiError, read_json, git
from .events import emit


def lookup(request):
    if request.get('toolkit') == 'owner':
        from .owner_state import OwnerSession
        session = OwnerSession(request)
        path = session.load().get('worktree')
    elif request.get('toolkit') == 'review':
        from .state import Session
        session = Session(request)
        path = session.manifest.get('worktree')
        # Old archive manifests still name the checkout that was subsequently
        # moved. The durable snapshot journal is authoritative for those paths.
        matches = set()
        for saved in session.paths():
            manifest = read_json(saved / 'manifest.json', {})
            entries = list(manifest.get('snapshot_history', []))
            if manifest.get('snapshot_update'):
                entries.append(manifest['snapshot_update'])
            for entry in entries:
                if entry.get('archive') and Path(entry['archive']).resolve() == session.path.resolve():
                    if entry.get('worktree'):
                        matches.add(entry['worktree'])
        if matches:
            path = next(iter(matches)) if len(matches) == 1 else None
        elif session.path != session.current and path:
            # Same-snapshot generations may share a retained checkout. An old
            # unmapped manifest must not redirect a historical view to new code.
            if not Path(path).is_dir():
                path = None
            else:
                result = git(['-C', path, 'rev-parse', 'HEAD'], check=False)
                if result.returncode or result.stdout.strip() != session.manifest.get('snapshot', {}).get('head'):
                    path = None
    else:
        raise ApiError('invalid_request', 'Choose the review or owner toolkit.')
    path = str(Path(path).resolve()) if path else None
    available = bool(path and Path(path).is_dir())
    return {'path': path, 'available': available,
            'message': '' if available else 'Checkout is unavailable or has not been created yet.'}


def announce(path, session, toolkit, generation=None):
    if path and Path(path).is_dir():
        path = str(Path(path).resolve())
        emit('progress', message='Checkout: ' + path, checkout=path,
             session=str(session), toolkit=toolkit, generation=generation)
