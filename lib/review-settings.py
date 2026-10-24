#!/usr/bin/env python3
"""Internal CLI configuration reader; explicit --resume may be outside XDG state."""
import json
import sys

from pr_review_api.common import ApiError, read_json
from pr_review_api.settings import DEFAULTS, from_manifest, validate

try:
    request = json.load(sys.stdin)
    saved = read_json(request["manifest"], {}) if request.get("manifest") else {}
    configuration = from_manifest(saved) if saved else dict(DEFAULTS)
    configuration.update(request.get("overrides", {}))
    print(json.dumps(validate(configuration)))
except (ApiError, OSError, ValueError, TypeError) as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
