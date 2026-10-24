"""Metadata-only CLI discovery. Never send a user prompt or return account data."""
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager

from .common import ApiError


@contextmanager
def connection(provider, timeout=30):
    command = (["codex", "app-server", "--listen", "stdio://"] if provider == "codex" else
               ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "user",
                "--settings", '{"disableAllHooks":true}', "--disable-slash-commands"])
    with tempfile.TemporaryDirectory(prefix="pr-review-models-") as cwd:
        try:
            child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        except FileNotFoundError as exc:
            raise ApiError("discovery_failed", f"{provider} is not installed. Install it or disable this reviewer.") from exc
        previous = {}
        def cancelled(_sig, _frame):
            raise ApiError("discovery_cancelled", "Model discovery cancelled.")
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline, pending = time.monotonic() + timeout, b""
        def send(value):
            child.stdin.write((json.dumps(value) + "\n").encode())
            child.stdin.flush()
        def receive():
            nonlocal pending
            while True:
                if b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        return value
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ApiError("discovery_timeout", f"{provider} model discovery timed out; refresh choices to retry.")
                    data = os.read(child.stdout.fileno(), 65536)
                    if not data:
                        raise ApiError("discovery_failed", f"{provider} stopped before returning model choices. Check CLI login and version, then retry.")
                    pending += data
                    if len(pending) > 4 * 1024 * 1024:
                        raise ApiError("discovery_failed", f"{provider} returned oversized model metadata.")
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, cancelled)
            yield send, receive
        finally:
            # Stop descendants too, even when the CLI already exited.
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_IGN)
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            selector.close()
            child.stdin.close()
            child.stdout.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def discover(request):
    provider = request.get("provider")
    if provider not in ("claude", "codex"):
        raise ApiError("invalid_request", "Model provider must be claude or codex.")
    raw = []
    with connection(provider) as (send, receive):
        if provider == "codex":
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "auto-review", "version": "0.1"}, "capabilities": {}}})
            while True:
                reply = receive()
                if reply.get("id") == 1:
                    if "error" in reply:
                        raise ApiError("discovery_failed", "Codex initialization failed; check CLI login and version.")
                    break
            send({"method": "initialized", "params": {}})
            cursor, seen, ident = None, set(), 2
            while True:
                params = {"limit": 100, "includeHidden": False}
                if cursor:
                    params["cursor"] = cursor
                send({"id": ident, "method": "model/list", "params": params})
                while True:
                    reply = receive()
                    if reply.get("id") == ident:
                        break
                if "error" in reply:
                    raise ApiError("discovery_failed", "Codex could not list models; check CLI login and version.")
                page = reply.get("result", {})
                raw.extend(page.get("data", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise ApiError("discovery_failed", "Codex returned a repeated model page; update the CLI and retry.")
                seen.add(cursor)
                ident += 1
        else:
            send({"type": "control_request", "request_id": "models", "request": {
                "subtype": "initialize", "hooks": {}, "sdkMcpServers": []}})
            while True:
                reply = receive()
                response = reply.get("response", {})
                if reply.get("type") == "control_response" and response.get("request_id") == "models":
                    if response.get("subtype") != "success":
                        raise ApiError("discovery_failed", "Claude initialization failed; check CLI login and version.")
                    raw = response.get("response", {}).get("models", [])
                    break
    choices, seen = [], set()
    for model in raw:
        name = model.get("model") if provider == "codex" else model.get("value")
        if not isinstance(name, str) or not name or name in seen or model.get("hidden") or model.get("disabled"):
            continue
        if provider == "claude" and model.get("supportsEffort") is False:
            continue
        efforts = ([e.get("reasoningEffort") for e in model.get("supportedReasoningEfforts", [])]
                   if provider == "codex" else model.get("supportedEffortLevels", []))
        efforts = list(dict.fromkeys(e for e in efforts if isinstance(e, str) and e))
        # This workflow requires explicit effort (including /code-review effort).
        # Do not invent levels when an older CLI omits capability metadata.
        if not efforts:
            continue
        seen.add(name)
        choices.append({"value": name, "label": model.get("displayName") or name, "efforts": efforts})
    if not choices:
        raise ApiError("discovery_failed", f"{provider} returned no usable models with effort metadata; update the CLI and retry.")
    return {"provider": provider, "models": choices}


def validate_selection(settings, catalogs):
    for provider in settings["reviewers"]:
        catalog = catalogs.get(provider, {})
        if catalog.get("provider") != provider:
            raise ApiError("invalid_request", f"Refresh {provider} choices before running.")
        model = next((m for m in catalog.get("models", []) if m["value"] == settings[provider + "_model"]), None)
        if not model or settings[provider + "_effort"] not in model["efforts"]:
            raise ApiError("invalid_request", f"Choose a listed {provider} model and one of its supported efforts.")
