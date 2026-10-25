"""One JSON request on stdin; NDJSON events and exactly one final result/error."""
import json
import sys

from .common import ApiError, PROTOCOL, emit, locked
from .commands import mutate, preview_comment
from .state import Session, discover
from . import operations, publication, settings, models, workflow, pulls


def dispatch(request):
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        raise ApiError("protocol_mismatch", "Use protocol version 1.", supported=[PROTOCOL])
    operation = request.get("command")
    if operation == "capabilities":
        return {"protocol": PROTOCOL, "features": ["generations", "inline-context", "drafts", "history", "inline-publication", "operations", "conversations", "followup", "review-status", "pull-requests"],
                "commands": ["discover", "pull-requests", "settings", "models", "read", "history", "diff", "file", "preview-comment", "mutate", "refresh", "prepare", "publish", "resolve", "run", "cancel", "adapt", "thread-state", "followup"]}
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
    session = Session(request)
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
    with locked(session.lock):
        state = session.load()
        session.check_write(request, state)
        if operation == "preview-comment":
            return preview_comment(session, state, request)
        if operation == "mutate":
            return mutate(session, state, request)
        # Never interleave CLI Git/network state mutations with publication.
        with locked(session.pr_lock):
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


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--terminal":
            from .terminal import terminal
            terminal(sys.argv[2])
            return
        raw = sys.stdin.readline(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ApiError("invalid_request", "Request is too large.")
        try:
            request = json.loads(raw)
        except ValueError as exc:
            raise ApiError("invalid_request", "Expected one JSON request line on stdin.") from exc
        emit("result", result=dispatch(request))
    except ApiError as exc:
        emit("error", error={"code": exc.code, "message": str(exc), **exc.details})
        sys.exit(1)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        emit("error", error={"code": "invalid_state", "message": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
