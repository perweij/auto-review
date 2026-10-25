"""Small standard-library primitives shared by API operations."""
import contextlib
from contextvars import ContextVar
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = 1


class ApiError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ApiError("invalid_artifact", f"Cannot read saved JSON: {path}") from exc


def atomic_json(path, value):
    from .storage import atomic_bytes
    # Preserve the existing API/follow-up JSON bytes while sharing durability.
    atomic_bytes(path, (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("utf-8"))


@contextlib.contextmanager
def locked(path, *, wait=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if not wait:
                    raise ApiError("busy", "Another operation is using this review; retry shortly.") from exc
                from .processes import _cancellation
                cancellation = _cancellation.get()
                if cancellation:
                    cancellation.check()
                import time
                time.sleep(0.05)
        yield


def command(argv, *, cwd=None, input_text=None, check=True, timeout=None):
    """Run a cancellable command; deadlines belong to the calling operation."""
    from .processes import run
    result = run(argv, cwd=cwd, input_bytes=(input_text or "").encode("utf-8"),
                 timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                      "GH_PROMPT_DISABLED": "1"})
    if check and result.returncode:
        raise ApiError("command_failed", result.stderr.strip() or f"{argv[0]} failed",
                       exit_code=result.returncode)
    return result


def git(args, *, cwd=None, check=True):
    return command(["git", "-c", "core.hooksPath=/dev/null", "-c",
                    "core.quotePath=false", *args], cwd=cwd, check=check)


def state_root():
    return (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pr-review").resolve()


_local_sessions = ContextVar('local_review_sessions', default=False)


@contextlib.contextmanager
def local_sessions():
    """Internal CLI capability; never derived from a protocol request field."""
    token = _local_sessions.set(True)
    try:
        yield
    finally:
        _local_sessions.reset(token)


def artifact_path(value):
    if not isinstance(value, str):
        raise ApiError("invalid_request", "A session returned by discover is required.")
    path = Path(value).resolve()
    if not _local_sessions.get() and not path.is_relative_to(state_root()):
        raise ApiError("invalid_session", "Session is outside the review state directory.")
    return path


def require_text(value, name, *, empty=False):
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ApiError("invalid_request", f"{name} must be {'a' if empty else 'a nonempty'} string without NUL.")
    return value
