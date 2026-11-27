"""Owned subprocess groups, incremental output, and bounded cancellation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import os
import re
import selectors
import signal
import subprocess
import time
import tempfile
import threading

from .common import ApiError
from .events import emit


class Cancelled(ApiError):
    def __init__(self, signum):
        super().__init__("cancelled", "Operation cancelled; completed reviews and saved drafts are retained.",
                         exit_code=128 + signum)
        self.signum = signum


@dataclass
class Cancellation:
    signum: int = 0

    def request(self, signum=signal.SIGTERM):
        if not self.signum:
            self.signum = signum

    def check(self):
        if self.signum:
            raise Cancelled(self.signum)


_cancellation = ContextVar("review_cancellation", default=None)


class Bookkeeping:
    """Serialises concurrent stages' Python work; a stage releases it only to wait."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def __enter__(self):
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *_):
        self._owner = None
        self._lock.release()

    def held(self):
        return self._owner == threading.get_ident()


_shared = ContextVar("shared_bookkeeping", default=None)


@contextmanager
def bookkeeping(lock):
    """Run this thread's stage under ``lock``; nested child waits release it."""
    token = _shared.set(lock)
    try:
        with lock:
            yield
    finally:
        _shared.reset(token)


@contextmanager
def released():
    """Let concurrent stages update shared state while this one waits on a child."""
    lock = _shared.get()
    if lock is None or not lock.held():
        yield
        return
    lock.__exit__()
    try:
        yield
    finally:
        lock.__enter__()


@contextmanager
def cancellation_scope():
    current = _cancellation.get()
    if current is not None:
        yield current
        return
    current = Cancellation()
    token = _cancellation.set(current)
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, lambda signum, _frame: current.request(signum))
        yield current
        current.check()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        _cancellation.reset(token)


@contextmanager
def shared_cancellation():
    """The caller's cancellation, or a local one for threads started without a scope."""
    current = _cancellation.get()
    if current is not None:
        yield current
        return
    current = Cancellation()
    token = _cancellation.set(current)
    try:
        yield current
    finally:
        _cancellation.reset(token)


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False


def stop_group(child, first=signal.SIGTERM, grace=2):
    """Reap the direct child and terminate descendants, including after its exit."""
    for sig in dict.fromkeys((first, signal.SIGTERM, signal.SIGKILL)):
        child.poll()
        if not signal_group(child.pid, 0):
            break
        if sig == signal.SIGKILL:
            try:
                emit('progress', message='Warning: Stopping remaining model processes with KILL.')
            except (BrokenPipeError, OSError):
                pass
        signal_group(child.pid, sig)
        deadline = time.monotonic() + (1 if sig == signal.SIGKILL else grace)
        while time.monotonic() < deadline:
            child.poll()
            if not signal_group(child.pid, 0):
                break
            with released():
                time.sleep(0.05)
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass  # Cleanup must remain bounded, even for a task stuck in kernel I/O.


def event_error(event):
    if event.get("type") == "error" and re.search("not a git repository", str(event.get("message", "")), re.I):
        return "git_repository_unavailable"
    if event.get("type") == "turn.failed":
        return "codex_turn_failed"
    if event.get("type") == "result" and event.get("is_error") is True:
        return "model_result_error"
    return "nonterminal_error" if event.get("type") == "error" else ""


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    events: list = field(default_factory=list)
    reason: str = ""
    process_exit_status: int = 0


def run(argv, *, cwd=None, env=None, input_bytes=b"", timeout=None, stdout_path=None,
        stderr_path=None, model_events=False, stage="", on_line=None, pass_fds=(), usage_call=None,
        max_output_bytes=None, capture_files=False, progress_label=""):
    """Run without a shell; log raw bytes while interpreting complete records."""
    if capture_files and (stdout_path is not None or stderr_path is not None or model_events or on_line or usage_call):
        raise ValueError('File capture is only for non-streaming metadata commands.')
    from .issue_resources import check as check_issue_resources, active as issue_resources_active
    check_issue_resources()
    cancellation = _cancellation.get() or Cancellation()
    cancellation.check()
    argv = [str(a) for a in argv]
    started = activity = heartbeat = time.monotonic()
    pending = {"stdout": bytearray(), "stderr": bytearray()}
    limit = os.environ.get('PR_REVIEW_MAX_EVENT_BYTES') or str(64 * 1024 * 1024)
    if (model_events or on_line) and not re.fullmatch(r'[1-9][0-9]*', limit):
        raise ApiError('invalid_request', 'PR_REVIEW_MAX_EVENT_BYTES must be positive integer bytes.')
    record_limit = int(limit) if model_events or on_line else 0
    captured = {"stdout": [], "stderr": []}
    output_bytes = 0
    files, events, reason, warned = {}, [], "", False
    capture = {}
    child = None
    stopped = False
    silence_warned = False
    drain_deadline = None
    from . import usage
    accounting = usage.begin_call(usage_call, env)
    from . import session_trace
    trace = session_trace.begin(stage, accounting, stdout_path) if usage_call else None
    completed_result = None
    try:
        for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            if path is not None:
                files[name] = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        if accounting is not None:
            check_issue_resources()
            cancellation.check()
            # Metadata reads do not consume the provider-process timeout.
            started = activity = heartbeat = time.monotonic()
        if capture_files:
            # Some CLIs exit with queued pipe writes still pending. Regular files
            # make metadata output synchronous without changing model streaming.
            for name in pending:
                capture[name] = tempfile.TemporaryFile()
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=capture.get('stdout', subprocess.PIPE),
                                 stderr=capture.get('stderr', subprocess.PIPE),
                                 start_new_session=True, close_fds=True, pass_fds=pass_fds)
        usage.observe(accounting, 'launched')
        with selectors.DefaultSelector() as selector:
            for name in (() if capture_files else pending):
                os.set_blocking(getattr(child, name).fileno(), False)
                selector.register(getattr(child, name), selectors.EVENT_READ, name)
            remaining = memoryview(input_bytes)
            if remaining:
                os.set_blocking(child.stdin.fileno(), False)
                selector.register(child.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                child.stdin.close()

            def line_received(name, raw):
                nonlocal reason, warned
                text = raw.decode("utf-8", errors="replace")
                if on_line is not None:
                    on_line(name, text)
                if not model_events or name != "stdout":
                    return
                try:
                    event = json.loads(text)
                except ValueError:
                    return
                if not isinstance(event, dict):
                    return
                usage.observe(accounting, 'event', event)
                failure = event_error(event)
                if failure == "nonterminal_error":
                    if not warned:
                        emit("progress", message=f"Warning: {stage} reported an error event and may be retrying.")
                        warned = True
                elif failure:
                    reason = reason or failure
                item = event.get('item')
                if event.get("type") in ("result", "turn.completed", "turn.failed", "error") or (
                        event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message"):
                    events.append(event)

            while selector.get_map() or child.poll() is None:
                cancellation.check()
                check_issue_resources()
                current = time.monotonic()
                if capture_files and max_output_bytes is not None:
                    if sum(os.fstat(stream.fileno()).st_size for stream in capture.values()) > max_output_bytes:
                        raise ApiError("output_limit", "Process exceeded its output byte limit.")
                if not stopped and timeout is not None and current - started >= timeout:
                    reason = "timeout"
                if reason and not stopped:
                    # Let an already completed provider retain its actual exit code.
                    try:
                        child.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        pass
                    if child.poll() is None:
                        detail = "exceeded its time limit" if reason == "timeout" else "emitted a terminal error"
                        emit("progress", message=f"Warning: {stage} {detail}; stopping the process.")
                        stop_group(child)
                    stopped = True
                    drain_deadline = time.monotonic() + 1
                if drain_deadline and time.monotonic() > drain_deadline:
                    break
                with released():
                    ready = selector.select(0.02 if not stage else 0.1)
                for key, _ in ready:
                    name = key.data
                    if name == "stdin":
                        try:
                            size = os.write(key.fd, remaining[:65536])
                            remaining = remaining[size:]
                        except BrokenPipeError:
                            remaining = memoryview(b"")
                        if not remaining:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    try:
                        data = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        if pending[name]:
                            line_received(name, pending[name])
                            pending[name].clear()
                        continue
                    output_bytes += len(data)
                    if max_output_bytes is not None and output_bytes > max_output_bytes:
                        raise ApiError("output_limit", "Process exceeded its output byte limit; partial logs are retained.")
                    activity = time.monotonic()
                    silence_warned = False
                    if name in files:
                        files[name].write(data)
                        files[name].flush()
                    else:
                        captured[name].append(data)
                    if on_line or (model_events and name == 'stdout'):
                        # Append each chunk once; long partial records must not
                        # cause quadratic copying. Stderr is only raw logging.
                        parts = data.split(b'\n')
                        for index, part in enumerate(parts):
                            if len(pending[name]) + len(part) > record_limit:
                                raise ApiError('output_limit',
                                    f'Model event exceeds {record_limit} bytes; raw logs are retained. '
                                    'PR_REVIEW_MAX_EVENT_BYTES can raise this limit.')
                            pending[name].extend(part)
                            if index < len(parts) - 1:
                                line_received(name, pending[name])
                                pending[name].clear()
                if stage and current - heartbeat >= (10 if issue_resources_active() else 5):
                    prefix = f"[{stage}] {progress_label}".rstrip()
                    emit("progress", message=f"{prefix} Process running: {int(current-started)}s elapsed; {max(0, int(current-activity))}s since last output (stdout/stderr)")
                    if current - activity >= 60 and not silence_warned:
                        emit("progress", message="Warning: No output for 60s; progress is unconfirmed. Ctrl-C stops this stage and retains completed reviews.")
                        silence_warned = True
                    heartbeat = current
                if child.poll() is not None and selector.get_map() and not stopped:
                    # A provider must not leave grandchildren holding its pipes open.
                    stop_group(child)
                    stopped = True
                    drain_deadline = time.monotonic() + 1
            raw_code = child.wait(timeout=2)
            if capture_files:
                for name, stream in capture.items():
                    output_bytes += os.fstat(stream.fileno()).st_size
                    if max_output_bytes is not None and output_bytes > max_output_bytes:
                        raise ApiError("output_limit", "Process exceeded its output byte limit.")
                    stream.seek(0)
                    captured[name].append(stream.read())
            code = 124 if reason == "timeout" else (raw_code if raw_code >= 0 else 128 - raw_code)
            if reason and not code:
                code = 1
            completed_result = Result(code, b"".join(captured["stdout"]).decode("utf-8", errors="replace"),
                          b"".join(captured["stderr"]).decode("utf-8", errors="replace"),
                          events, reason, raw_code if raw_code >= 0 else 128 - raw_code)
            return completed_result
    except OSError as exc:
        raise ApiError("command_failed", f"Cannot complete {argv[0]}: {exc}") from exc
    finally:
        if child is not None:
            stop_group(child, cancellation.signum or signal.SIGTERM)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe:
                    pipe.close()
        for stream in [*files.values(), *capture.values()]:
            stream.close()
        usage.observe(accounting, 'finish', completed_result)
        session_trace.end(trace, completed_result)
