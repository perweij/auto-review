#!/usr/bin/env bash
# Opt-in live replay of the frozen PR4 corpus through the shared review engine.
#
# One full `pr-review` run (independent Claude and Codex reviews, then Codex
# synthesis) over base f7f0f2e and head f5e0321, with the historical description,
# discussion and CI withheld. It uses `C-feature-synthesised`'s configuration:
# Claude Opus and Codex Sol at high effort, Codex synthesis. Real Claude and Codex
# CLIs run, so it consumes provider usage; only GitHub is mocked and nothing is
# published. Compare the result with docs/evaluations/pr4 (that case's 8 supported
# behavioral findings and 2 supported non-blocking observations) and record the
# adjudication under docs/evaluations/review-engine/.
#
#   tests/replay-pr4.sh [ARTIFACT_DIRECTORY]
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Not BASE: the sourced fixture assigns that name.
PR4_BASE=f7f0f2e2df548186b3b85d5f420ad0424af25655
PR4_HEAD=f5e0321ca179d4c4a434ec456cb404b3cedd7486
for sha in "$PR4_BASE" "$PR4_HEAD"; do
    git -C "$ROOT" cat-file -e "$sha^{commit}" 2>/dev/null || { printf 'The PR4 corpus commit %s is not in this repository.\n' "$sha" >&2; exit 2; }
done
command -v claude codex >/dev/null || { echo 'claude and codex are required.' >&2; exit 2; }
OUT=${1:-$(mktemp -d "${TMPDIR:-/tmp}/pr4-replay-XXXXXX")}
mkdir -p "$OUT"
printf 'PR4 replay artifacts (retained): %s\n' "$OUT"
# Real authentication and executables; setup replaces HOME and the provider homes with fixtures.
REAL_HOME=$HOME REAL_PATH=$PATH
REAL_CODEX_HOME=${CODEX_HOME-} REAL_CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR-}
bats_require_minimum_version() { :; }
# shellcheck source=helpers/fixture.bash
source "$ROOT/tests/helpers/fixture.bash"
export BATS_TEST_DIRNAME="$ROOT/tests" BATS_TEST_TMPDIR="$OUT/fixture"
setup
trap 'teardown' EXIT
# Replace the toy history, whose commits this repository does not have, with the frozen snapshot.
git --git-dir="$FIXTURE/remote.git" update-ref -d refs/heads/main
git --git-dir="$FIXTURE/remote.git" update-ref -d refs/pull/7/head
git -C "$ROOT" push -q --force "$FIXTURE/remote.git" "$PR4_BASE:refs/heads/main" "$PR4_HEAD:refs/pull/7/head"
# Only GitHub is mocked: the real review CLIs stay on PATH.
rm -f "$FIXTURE/bin/claude" "$FIXTURE/bin/codex"
export HOME=$REAL_HOME PATH="$FIXTURE/bin:$REAL_PATH"
if [[ -n $REAL_CODEX_HOME ]]; then export CODEX_HOME=$REAL_CODEX_HOME; else unset CODEX_HOME; fi
if [[ -n $REAL_CLAUDE_CONFIG_DIR ]]; then export CLAUDE_CONFIG_DIR=$REAL_CLAUDE_CONFIG_DIR; else unset CLAUDE_CONFIG_DIR; fi
export PR_REVIEW_MODEL_TIMEOUT=${PR_REVIEW_MODEL_TIMEOUT:-1800}
printf '[]\n' > "$FIXTURE/issues.json"        # discussion withheld
{
    printf 'base %s\nhead %s\n' "$PR4_BASE" "$PR4_HEAD"
    claude --version; codex --version
} > "$OUT/environment.txt"
status=0
"$CLI" 7 --no-publish --claude-model opus --claude-effort high --codex-model gpt-6.1-sol --codex-effort high \
    --processing-provider codex > "$OUT/run.log" 2>&1 || status=$?
printf 'pr-review exit status: %s (log: %s)\n' "$status" "$OUT/run.log"
directory=$(run_directory)
mkdir -p "$OUT/run"
cp -a "$directory/." "$OUT/run/" 2>/dev/null || true
if [[ -f $directory/reviews/synthesis.json ]]; then
    printf '\nSynthesis findings (compare with docs/evaluations/pr4/coverage-matrix.json):\n'
    jq -r '.findings[] | "\(.id)\t\(.validity)\t\(if .blocking then "blocking" else "non-blocking" end)\t\(.severity)\t\(.title)"' "$directory/reviews/synthesis.json"
    jq '{recommendation, blocking_findings, findings: (.findings|length)}' "$directory/reviews/synthesis.json"
fi
jq -r '.phases | to_entries[] | "\(.key)\t\(.value.status)\t\(.value.model // "")"' "$directory/manifest.json" || true
exit "$status"
