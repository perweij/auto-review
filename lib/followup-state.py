#!/usr/bin/env python3
"""Internal adapter for the shared snapshot/model orchestrator."""
import sys
from pr_review_api.common import ApiError
from pr_review_api.followup import cli

try:
    cli(sys.argv[1:])
except (ApiError, OSError, ValueError) as exc:
    print(f"Error: {exc}", file=sys.stderr)
    sys.exit(1)
