#!/usr/bin/env bash
# Opt-in live model smoke test. No real GitHub requests or publication.
# Uses existing CLI logins, incurs model usage, and preserves all artifacts.
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export FIXTURE
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/pr-review-smoke-XXXXXX")
mkdir -p "$FIXTURE"/{bin,state}
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$FIXTURE/gitconfig"
export GIT_AUTHOR_NAME=Smoke GIT_AUTHOR_EMAIL=smoke@example.com GIT_COMMITTER_NAME=Smoke GIT_COMMITTER_EMAIL=smoke@example.com
git init -q --bare "$FIXTURE/remote.git"
git init -q -b main "$FIXTURE/checkout"
cd -- "$FIXTURE/checkout"
printf 'def total(price, quantity):\n    """Return total price for quantity items."""\n    return price * quantity\n' > price.py
git add price.py
git commit -qm base
base=$(git rev-parse HEAD)
printf 'def total(price, quantity):\n    """Return total price for quantity items."""\n    return price + quantity\n' > price.py
git commit -qam 'Change total calculation'
git push -q "$FIXTURE/remote.git" "$base:refs/heads/main" HEAD:refs/pull/7/head
git remote add upstream https://github.com/org/repo.git
git config --global url."$FIXTURE/remote.git".insteadOf https://github.com/org/repo.git
ln -s "$ROOT/tests/helpers/mock-cli" "$FIXTURE/bin/gh"
export PATH="$FIXTURE/bin:$PATH" XDG_STATE_HOME="$FIXTURE/state"
printf 'Live model smoke fixture (retained): %s\n' "$FIXTURE"
"$ROOT/bin/pr-review" 7 --no-publish
