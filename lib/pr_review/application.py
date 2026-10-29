"""Shared application operations for the terminal and protocol adapters."""

from .common import ApiError, PROTOCOL, locked
from .commands import mutate, preview_comment
from .state import Session, discover
from . import operations, publication, settings, models, workflow, pulls


def dispatch(request):
    operation = request.get("command")
    if isinstance(operation, str) and (operation.startswith("owner-") or operation == "toolkit"):
        from .owner import dispatch as owner_dispatch
        return owner_dispatch(request)
    if operation == "capabilities":
        from .owner import COMMANDS
        return {"protocol": PROTOCOL, "features": ["generations", "inline-context", "drafts", "history", "inline-publication", "operations", "conversations", "followup", "review-status", "pull-requests", "toolkits", "owner", "suggested-changes", "automatic-review"],
                "commands": ["discover", "pull-requests", "settings", "models", "read", "review-log", "history", "diff", "file", "preview-comment", "mutate", "refresh", "prepare", "publish", "resolve", "run", "automatic-review", "verify-suggestions", "cancel", "adapt", "thread-state", "followup", *COMMANDS]}
    if operation == "settings":
        return settings.resolve(request)
    if operation == "models":
        return models.discover(request)
    if operation == "discover":
        return discover(request)
    if operation == "pull-requests":
        return pulls.list_pulls(request)
    if operation == "cancel":
        return operations.cancel(request)
    if operation == "run":
        return operations.run(request)
    if operation == 'automatic-review':
        from .automation import run
        if request.get('followup_threads') or request.get('no_publish'):
            raise ApiError('invalid_request', 'Automatic publication requires a full review.')
        if request.get('session'):
            session = Session(request)
            request = {**request, 'pr': session.manifest['pr'], 'repository': session.manifest['repository'],
                       'cwd': publication.checkout(session)}
        return run(request)
    session = Session(request)
    if operation == 'verify-suggestions':
        from .suggestions import verify_operation
        return verify_operation(session, request)
    if operation == 'review-log':
        from .automation import review_log
        return review_log(session, request)
    if operation == "followup":
        with locked(session.lock):
            session.check_write(request, session.load())
        return operations.followup(session, request)
    if operation == "history":
        return session.history()
    if operation == "read":
        return session.view(session.load())
    if operation == "diff":
        if request.get("path"):
            return session.git.diff(request["path"])
        return {"files": session.git.files(), "diffs": [session.git.diff(f["path"]) for f in session.git.files()]}
    if operation == "file":
        return session.git.source(request.get("path"), request.get("side", "RIGHT"), request.get("commit"))
    if operation == "adapt":
        with locked(session.lock):
            state = session.load()
            session.check_write(request, state)
        return operations.adapt(session, state, request)
    if operation in ('preview-comment', 'mutate'):
        with locked(session.lock):
            state = session.load()
            session.check_write(request, state)
            return (preview_comment if operation == 'preview-comment' else mutate)(session, state, request)
    # Global order: PR lock before a generation's draft transaction lock.
    with locked(session.pr_lock), locked(session.lock):
        state = session.load()
        session.check_write(request, state)
        if operation == "thread-state":
            return workflow.thread_state(session, state, request)
        if operation == "refresh":
            affected = publication.refresh(session, state)
            return {"review": session.view(state), "affected": affected}
        if operation == "prepare":
            return publication.prepare(session, state)
        if operation == "publish":
            return publication.publish(session, state, request)
        if operation == "resolve":
            publication.recover(session, state)
            return {"review": session.view(state)}
    raise ApiError("invalid_request", "Unknown API command.")
