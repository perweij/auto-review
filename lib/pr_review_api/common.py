"""Small standard-library primitives shared by API operations."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

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
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ApiError("invalid_artifact", f"Cannot read saved JSON: {path}") from exc


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".api-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ApiError("busy", "Another operation is using this review; retry shortly.") from exc
        yield


def command(argv, *, cwd=None, input_text=None, check=True, timeout=90):
    try:
        result = subprocess.run([str(x) for x in argv], cwd=cwd, input=input_text,
                                text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "GH_PROMPT_DISABLED": "1"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError("command_failed", f"Cannot complete {argv[0]}: {exc}") from exc
    if check and result.returncode:
        raise ApiError("command_failed", result.stderr.strip() or f"{argv[0]} failed",
                       exit_code=result.returncode)
    return result


def git(args, *, cwd=None, check=True):
    return command(["git", "-c", "core.hooksPath=/dev/null", "-c",
                    "core.quotePath=false", *args], cwd=cwd, check=check)


def state_root():
    return (Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pr-review").resolve()


def artifact_path(value):
    if not isinstance(value, str):
        raise ApiError("invalid_request", "A session returned by discover is required.")
    path = Path(value).resolve()
    if not path.is_relative_to(state_root()):
        raise ApiError("invalid_session", "Session is outside the review state directory.")
    return path


def emit(kind, **fields):
    print(json.dumps({"protocol": PROTOCOL, "type": kind, **fields}, ensure_ascii=True), flush=True)


def require_text(value, name, *, empty=False):
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ApiError("invalid_request", f"{name} must be {'a' if empty else 'a nonempty'} string without NUL.")
    return value
