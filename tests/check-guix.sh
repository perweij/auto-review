#!/usr/bin/env bash
# Build with the caller's channel pin, then test only installed store artifacts.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
channels=${1:-}
if [[ -z "$channels" || ! -f "$channels" ]]; then
  echo "Usage: make check-guix GUIX_CHANNELS=/path/to/channels.scm" >&2
  exit 2
fi
channels=$(realpath -- "$channels")
guix_command=${GUIX:-guix}
if ! command -v "$guix_command" >/dev/null; then
  echo "Guix is required for the installed-package acceptance check." >&2
  exit 2
fi

frontend=$("$guix_command" time-machine -C "$channels" -- build -f "$repo/guix.scm")
[[ "$frontend" == /gnu/store/* && -d "$frontend/share/emacs/site-lisp" ]]
temporary=$(mktemp -d -t auto-review-guix.XXXXXXXX)
trap 'rm -rf -- "$temporary"' EXIT
cp -- "$repo/tests/guix-installed.el" "$repo/tests/guix-installed.py" "$temporary/"
mkdir -p "$temporary/home" "$temporary/state" "$temporary/cache" "$temporary/config"
cd -- "$temporary"

# Use a fresh profile and HOME; no source load-path, providers or credentials.
"$guix_command" time-machine -C "$channels" -- \
  shell --pure -f "$repo/guix.scm" emacs coreutils -- \
  env HOME="$temporary/home" XDG_STATE_HOME="$temporary/state" \
      XDG_CACHE_HOME="$temporary/cache" XDG_CONFIG_HOME="$temporary/config" \
      AUTO_REVIEW_FRONTEND="$frontend" \
      AUTO_REVIEW_RUNTIME_TEST="$temporary/guix-installed.py" \
      emacs --batch -l "$temporary/guix-installed.el" -f ert-run-tests-batch-and-exit
