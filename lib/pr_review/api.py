"""Protocol 1 transport; business operations live in application."""
import json
import sys
from .common import ApiError, PROTOCOL
from . import application
from .events import event_sink
from .processes import cancellation_scope


def emit(kind, **fields):
    print(json.dumps({"protocol": PROTOCOL, "type": kind, **fields}, ensure_ascii=True), flush=True)


def dispatch(request):
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        raise ApiError("protocol_mismatch", "Use protocol version 1.", supported=[PROTOCOL])
    with event_sink(emit), cancellation_scope():
        return application.dispatch(request)


def main():
    try:
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
