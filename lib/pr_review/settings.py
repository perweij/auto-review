"""Review configuration, including read-only migration of legacy manifests."""
import copy

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
CONFIG_KEYS = (*DEFAULTS, "version", "reviewer_slots", "processing")


def from_manifest(manifest):
    models = manifest.get("models", {})
    result = dict(LEGACY_DEFAULTS)
    result["synthesis_tests"] = manifest.get("phases", {}).get("synthesis", {}).get("testing_policy", "focused")
    for provider in ("claude", "codex"):
        result[provider + "_model"] = models.get(provider) or LEGACY_DEFAULTS[provider + "_model"]
        result[provider + "_effort"] = models.get(provider + "_effort", models.get("effort", "high"))
    config = manifest.get("configuration", {})
    result.update(config)
    # Legacy single-reviewer validation always used the sole reviewer.
    if "reviewer_slots" not in config:
        reviewers = result.get("reviewers", [])
        if isinstance(reviewers, list) and len(reviewers) == 1:
            result["processing_provider"] = reviewers[0]
        elif reviewers in (["claude", "codex"], ["codex", "claude"]):
            # The old validator always normalized a mixed pair to this order.
            result["reviewers"] = ["claude", "codex"]
    return result


def legacy_review_artifacts(manifest):
    """Describe provider-named independent reviews without changing their identity.

    The old pipeline only ran independent phases for a mixed pair, in Claude,
    Codex order. Single-reviewer runs used synthesis directly. The saved
    choices travel with the mapping so later configuration changes cannot make
    an old artifact appear to belong to a different slot selection.
    """
    config = manifest.get('configuration', {})
    if 'reviewer_slots' in config:
        return None
    resolved = validate(from_manifest(manifest))
    if set(resolved['reviewers']) != {'claude', 'codex'} or len(resolved['reviewers']) != 2:
        return None
    return {
        f'reviewer-{index}': {
            'phase': provider,
            'output': filename,
            'provider': provider,
            'model': resolved[provider + '_model'],
            'effort': resolved[provider + '_effort'],
        }
        for index, (provider, filename) in enumerate((
            ('claude', 'reviews/claude-independent.md'),
            ('codex', 'reviews/codex.md')), 1)
    }


def validate(settings):
    """Normalize provider-based inputs or a versioned slot configuration."""
    result = {k: settings.get(k, default) for k, default in DEFAULTS.items()}
    if type(result['suggested_changes']) is not bool:
        raise ApiError('invalid_request', 'suggested_changes must be a boolean.')
    if result["synthesis_tests"] not in ("focused", "full"):
        raise ApiError("invalid_request", "synthesis_tests must be focused or full.")
    def choice(value):
        if not isinstance(value, dict) or value.get("provider") not in ("claude", "codex"):
            raise ApiError("invalid_request", "Choose a Claude or Codex provider.")
        for key in ("model", "effort"):
            v = value.get(key)
            if not isinstance(v, str) or not v.strip() or v.startswith("-") or any(ord(c) < 32 for c in v):
                raise ApiError("invalid_request", "Invalid model or effort option.")
        return {key: value[key] for key in ("provider", "model", "effort")}
    for provider in ("claude", "codex"):
        choice({"provider": provider, "model": result[provider + "_model"],
                "effort": result[provider + "_effort"]})
    if "version" in settings and (type(settings["version"]) is not int or settings["version"] != 1):
        raise ApiError("invalid_request", "Unsupported configuration version.")
    slots = settings.get("reviewer_slots")
    if "reviewer_slots" not in settings:
        reviewers = result["reviewers"]
        if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 2 or any(p not in ("claude", "codex") for p in reviewers):
            raise ApiError("invalid_request", "Choose one or two Claude or Codex reviewers.")
        slots = [{"id": f"reviewer-{i}", "provider": p, "model": result[p + "_model"],
                  "effort": result[p + "_effort"]} for i, p in enumerate(reviewers, 1)]
    if not isinstance(slots, list) or not 1 <= len(slots) <= 2:
        raise ApiError("invalid_request", "Choose one or two reviewer slots.")
    normalized = []
    for i, slot in enumerate(slots, 1):
        if not isinstance(slot, dict) or slot.get("id") != f"reviewer-{i}":
            raise ApiError("invalid_request", "Reviewer slot IDs must be reviewer-1 and reviewer-2 in order.")
        normalized.append({"id": slot["id"], **choice(slot)})
    processing = settings.get("processing")
    if "processing" not in settings:
        provider = result["processing_provider"]
        if "processing_provider" not in settings and len(normalized) == 1:
            provider = normalized[0]["provider"]
        if provider not in ("claude", "codex"):
            raise ApiError("invalid_request", "Choose a Claude or Codex processing provider.")
        processing = {"provider": provider, "model": result[provider + "_model"],
                      "effort": result[provider + "_effort"]}
    processing = choice(processing)
    result.update(version=1, reviewer_slots=normalized, processing=processing,
                  reviewers=[slot["provider"] for slot in normalized],
                  processing_provider=processing["provider"])
    # Preserve provenance for the legacy provider fields emitted alongside the
    # canonical choices. They are defaults for old callers, not slot overrides
    # when a resolved configuration is submitted again.
    result["_compatibility"] = {provider + "_" + field: result[provider + "_" + field]
                                for provider in ("claude", "codex")
                                for field in ("model", "effort")}
    result["_compatibility"].update(reviewers=list(result["reviewers"]),
                                    processing_provider=result["processing_provider"])
    return result


def request_defaults(request):
    defaults = request.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ApiError("invalid_request", "defaults must be an object.")
    return {key: value for key, value in defaults.items() if value is not None}


def initial_settings(request):
    """Expand new-review defaults consistently for CLI and API entry points."""
    settings = dict(DEFAULTS)
    defaults = request_defaults(request)
    settings.update({k: v for k, v in defaults.items() if k in DEFAULTS and v is not None})
    for name in PRESETS:
        if type(request.get(name, False)) is not bool:
            raise ApiError('invalid_request', f'{name} must be a boolean.')
    selected = [name for name in PRESETS if request.get(name)]
    if len(selected) > 1:
        raise ApiError('invalid_request', 'Choose economical or expensive, not both.')
    if selected:
        settings.update(PRESETS[selected[0]])
    # Legacy single-reviewer requests implicitly process with that reviewer.
    # Explicit processing settings, including user defaults, always win.
    selected_reviewers = request.get("reviewers", defaults.get("reviewers"))
    selected_slots = request.get("reviewer_slots", defaults.get("reviewer_slots"))
    if selected_slots is not None and isinstance(selected_slots, list):
        selected_reviewers = [slot.get("provider") for slot in selected_slots if isinstance(slot, dict)]
    if (isinstance(selected_reviewers, list) and len(selected_reviewers) == 1
            and "processing_provider" not in request and "processing" not in request
            and "processing_provider" not in defaults and "processing" not in defaults):
        settings["processing_provider"] = selected_reviewers[0]
    settings.update({k: v for k, v in defaults.items() if k in ("version", "reviewer_slots", "processing")})
    return settings


def apply_overrides(settings, request, *, fresh=False):
    """Apply individual choices over the already resolved defaults or saved state."""
    overrides = {k: request[k] for k in CONFIG_KEYS if k in request}
    compatibility = request.get("_compatibility")
    if isinstance(compatibility, dict) and "reviewer_slots" in request and "processing" in request:
        for key in compatibility:
            if key in ("claude_model", "claude_effort", "codex_model", "codex_effort",
                       "reviewers", "processing_provider") and key in overrides and overrides[key] == compatibility[key]:
                overrides.pop(key, None)
    current = validate(settings)
    slots = current["reviewer_slots"]
    if "reviewer_slots" in overrides:
        slots = validate({**current, "reviewer_slots": overrides["reviewer_slots"]})["reviewer_slots"]
    if "reviewers" in overrides:
        reviewers = overrides["reviewers"]
        if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 2 or any(p not in ("claude", "codex") for p in reviewers):
            raise ApiError("invalid_request", "Choose one or two Claude or Codex reviewers.")
        slots = [dict(slots[i]) if i < len(slots) and slots[i]["provider"] == provider else
                 {"id": f"reviewer-{i+1}", "provider": provider,
                  "model": current[provider + "_model"], "effort": current[provider + "_effort"]}
                 for i, provider in enumerate(reviewers)]
    slots = [dict(slot) for slot in slots]
    for i, slot in enumerate(slots, 1):
        selected_provider = request.get(f"reviewer_{i}_provider", slot.get("provider"))
        if selected_provider != slot.get("provider"):
            if selected_provider not in ("claude", "codex"):
                raise ApiError("invalid_request", "Choose a Claude or Codex provider.")
            slot.update(provider=selected_provider, model=current[selected_provider + "_model"],
                        effort=current[selected_provider + "_effort"])
        provider = slot.get("provider")
        for field in ("model", "effort"):
            key = str(provider) + "_" + field
            if key in overrides:
                slot[field] = overrides[key]
        for field in ("provider", "model", "effort"):
            key = f"reviewer_{i}_{field}"
            if key in request:
                slot[field] = request[key]
    processing = dict(current["processing"])
    defaults = request_defaults(request)
    if (fresh and len(slots) == 1 and
            not any(k in request or k in defaults for k in ("processing", "processing_provider"))):
        provider = slots[0]["provider"]
        if provider not in ("claude", "codex"):
            raise ApiError("invalid_request", "Choose a Claude or Codex provider.")
        processing = {"provider": provider, "model": current[provider + "_model"],
                      "effort": current[provider + "_effort"]}
    if "processing" in overrides:
        processing = validate({**current, "processing": overrides["processing"]})["processing"]
    provider = overrides.get("processing_provider", processing["provider"])
    if provider not in ("claude", "codex"):
        raise ApiError("invalid_request", "Choose a Claude or Codex processing provider.")
    if provider != processing["provider"]:
        processing = {"provider": provider, "model": current[provider + "_model"],
                      "effort": current[provider + "_effort"]}
    for field in ("model", "effort"):
        key = provider + "_" + field
        if key in overrides:
            processing[field] = overrides[key]
        if "processing_" + field in request:
            processing[field] = request["processing_" + field]
    settings.update(overrides)
    settings.update(reviewer_slots=slots, processing=processing)


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
        settings.update(validate(from_manifest(manifest)))
    apply_overrides(settings, request, fresh=not session_path)
    return {"settings": validate(settings), "session": str(session_path) if session_path else None}


REVIEWER_FLAGS = ("reviewers", "reviewer_slots", "processing", "processing_provider", "processing_model",
                  "processing_effort", "claude_model", "codex_model", "claude_effort", "codex_effort",
                  "synthesis_tests", *(f"reviewer_{i}_{field}" for i in (1, 2) for field in ("provider", "model", "effort")))


def explicit_reviewer_flags(request):
    """Whether a request explicitly chose reviewer, synthesis or preset options."""
    return (any(request.get(key) is not None for key in REVIEWER_FLAGS)
            or any(request.get(name) is True for name in PRESETS))


def final_review_from(resolved):
    """The Owner final-review contract of a validated reviewer configuration."""
    return {"reviewer_slots": copy.deepcopy(resolved["reviewer_slots"]), "synthesis": dict(resolved["processing"]),
            "synthesis_tests": resolved["synthesis_tests"]}


def validate_final_review(value):
    """Normalize ``{reviewer_slots, synthesis, synthesis_tests}`` with the shared slot rules."""
    if not isinstance(value, dict) or "reviewer_slots" not in value or "synthesis" not in value:
        raise ApiError("invalid_request", "final_review needs reviewer_slots and a synthesis choice.")
    return final_review_from(validate({"reviewer_slots": value["reviewer_slots"], "processing": value["synthesis"],
                                       "synthesis_tests": value.get("synthesis_tests", "focused")}))


def derive_final_review(review):
    """A file or saved session without ``final_review`` stays single-reviewer.

    Its task-check choice is both the sole reviewer slot and the synthesis choice,
    so Claude-only and Codex-only users need no second provider. The value is
    computed when needed and never written back.
    """
    return {"reviewer_slots": [{"id": "reviewer-1", **{key: review[key] for key in ("provider", "model", "effort")}}],
            "synthesis": {key: review[key] for key in ("provider", "model", "effort")}, "synthesis_tests": "focused"}


def final_review_configuration(final_review):
    """A validated reviewer-style configuration for the shared engine."""
    return validate({"reviewer_slots": final_review["reviewer_slots"], "processing": final_review["synthesis"],
                     "synthesis_tests": final_review.get("synthesis_tests", "focused")})
