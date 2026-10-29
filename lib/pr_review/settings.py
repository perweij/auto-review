"""Review configuration, including read-only migration of legacy manifests."""
from .common import ApiError, read_json
from .state import Session, discover

DEFAULTS = {"reviewers": ["claude", "codex"], "processing_provider": "codex",
            "claude_model": "opus", "codex_model": "gpt-6.1-sol",
            "claude_effort": "high", "codex_effort": "high", "synthesis_tests": "focused",
            "suggested_changes": False}

# Manifests predating explicit configuration used these settings. New defaults
# must not silently change their providers or invalidate resumable work.
LEGACY_DEFAULTS = {**DEFAULTS, "processing_provider": "claude",
                   "claude_model": "fable", "codex_model": "gpt-6-astra"}

ECONOMICAL = {"reviewers": ["claude", "codex"], "processing_provider": "codex",
              "claude_model": "haiku", "codex_model": "gpt-6.1-terra",
              "claude_effort": "high", "codex_effort": "high"}
EXPENSIVE = {**ECONOMICAL, "claude_model": "fable", "codex_model": "gpt-6-astra"}
PRESETS = {"economical": ECONOMICAL, "expensive": EXPENSIVE}


def from_manifest(manifest):
    models = manifest.get("models", {})
    result = dict(LEGACY_DEFAULTS)
    result["synthesis_tests"] = manifest.get("phases", {}).get("synthesis", {}).get("testing_policy", "focused")
    for provider in ("claude", "codex"):
        result[provider + "_model"] = models.get(provider) or LEGACY_DEFAULTS[provider + "_model"]
        result[provider + "_effort"] = models.get(provider + "_effort", models.get("effort", "high"))
    result.update(manifest.get("configuration", {}))
    return result


def validate(settings):
    result = {k: settings.get(k, default) for k, default in DEFAULTS.items()}
    if type(result['suggested_changes']) is not bool:
        raise ApiError('invalid_request', 'suggested_changes must be a boolean.')
    reviewers = result["reviewers"]
    if (not isinstance(reviewers, list) or not reviewers or
            any(p not in ("claude", "codex") for p in reviewers) or len(set(reviewers)) != len(reviewers)):
        raise ApiError("invalid_request", "Choose Claude, Codex, or both reviewers.")
    result["reviewers"] = [p for p in ("claude", "codex") if p in reviewers]
    if len(reviewers) == 1:
        result["processing_provider"] = reviewers[0]
    if result["processing_provider"] not in reviewers:
        raise ApiError("invalid_request", "The processing provider must be an enabled reviewer.")
    if result["synthesis_tests"] not in ("focused", "full"):
        raise ApiError("invalid_request", "synthesis_tests must be focused or full.")
    for key in ("claude_model", "codex_model", "claude_effort", "codex_effort"):
        value = result[key]
        if not isinstance(value, str) or not value.strip() or value.startswith("-") or any(ord(c) < 32 for c in value):
            raise ApiError("invalid_request", "Invalid model or effort option.")
    return result


def initial_settings(request):
    """Expand new-review defaults consistently for CLI and API entry points."""
    settings = dict(DEFAULTS)
    defaults = request.get("defaults", {})
    settings.update({k: v for k, v in defaults.items() if k in DEFAULTS and v is not None})
    for name in PRESETS:
        if type(request.get(name, False)) is not bool:
            raise ApiError('invalid_request', f'{name} must be a boolean.')
    selected = [name for name in PRESETS if request.get(name)]
    if len(selected) > 1:
        raise ApiError('invalid_request', 'Choose economical or expensive, not both.')
    if selected:
        settings.update(PRESETS[selected[0]])
    return settings


def resolve(request):
    settings = initial_settings(request)
    session_path = request.get("session")
    if not session_path and request.get("pr"):
        matches = discover({**request, "pr": str(request["pr"]).rstrip("/").split("/")[-1]})["sessions"]
        if len(matches) > 1:
            paths = "\n".join("  " + match["session"] for match in matches)
            raise ApiError("ambiguous_session", "Multiple saved runs match this checkout and PR; select one with --resume RUN_DIRECTORY:\n" + paths)
        if matches:
            session_path = matches[0]["session"]
    if session_path:
        session = Session({"session": session_path})
        manifest = read_json((session.pointer(".pending-review") or session.path) / "manifest.json", {})
        settings.update(from_manifest(manifest))
    settings.update({k: request[k] for k in DEFAULTS if k in request})
    return {"settings": validate(settings), "session": str(session_path) if session_path else None}
