#!/usr/bin/env bash
# Reuse the CLI's complete discussion/integrity checks; invoked only by the API.
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=core.sh
source "$ROOT/lib/core.sh"
# shellcheck source=repository.sh
source "$ROOT/lib/repository.sh"
# shellcheck source=models.sh
source "$ROOT/lib/models.sh"
# shellcheck source=discussion.sh
source "$ROOT/lib/discussion.sh"
# shellcheck source=publication.sh
source "$ROOT/lib/publication.sh"
operation=$1
RUN_DIR=$2
EVENT=${3:-COMMENT}
COMMON_DIR=$(jq -er .git_common_dir "$RUN_DIR/manifest.json")
WORKTREE=$(jq -er .worktree "$RUN_DIR/manifest.json")
PR=$(jq -er .pr "$RUN_DIR/manifest.json")
RUN_ID=$(jq -er .run_id "$RUN_DIR/manifest.json")
REPO_ARG=$(jq -er .repository "$RUN_DIR/manifest.json")
TARGET=$PR
HEAD_SHA=$(jq -er .snapshot.head "$RUN_DIR/manifest.json")
BASE_SHA=$(jq -er .snapshot.base "$RUN_DIR/manifest.json")
MERGE_BASE=$(jq -er .snapshot.merge_base "$RUN_DIR/manifest.json")
VIEWER=$(jq -er .viewer "$RUN_DIR/manifest.json")
SCRATCH=$(mktemp -d)
trap 'rm -rf -- "$SCRATCH"' EXIT
resolve_repository
case $operation in
    refresh)
        fetch_pr_metadata "$RUN_DIR/publication/pr-latest.json"
        collect_discussion
        $DISCUSSION_READY || die 'Complete GitHub discussion is unavailable'
        collect_review_status
        ;;
    validate)
        current_viewer=$(gh api --hostname "$HOST" user --jq .login)
        [[ ${current_viewer,,} == "${VIEWER,,}" ]] || die 'GitHub account changed since this review; start a new review with that account'
        revalidate_publication
        ;;
    adapt)
        # No repository tools: only the supplied finding and full conversation.
        input=$4
        provider=$(jq -r '.configuration.processing_provider // "claude"' "$RUN_DIR/manifest.json")
        model=$(jq -r --arg p "$provider" '.configuration[$p+"_model"] // .models[$p] // (if $p=="claude" then "fable" else "gpt-6-astra" end)' "$RUN_DIR/manifest.json")
        effort=$(jq -r --arg p "$provider" '.configuration[$p+"_effort"] // .models[$p+"_effort"] // .models.effort // "high"' "$RUN_DIR/manifest.json")
        model_environment "$provider"
        prefix=()
        if [[ -n ${PR_REVIEW_MODEL_TIMEOUT:-} ]]; then
            [[ $PR_REVIEW_MODEL_TIMEOUT =~ ^[1-9][0-9]*$ ]] || die 'PR_REVIEW_MODEL_TIMEOUT must be positive seconds'
            prefix=(timeout --signal=TERM --kill-after=5s "${PR_REVIEW_MODEL_TIMEOUT}s")
        fi
        rm -rf -- "$SCRATCH"
        trap - EXIT
        if [[ $provider == codex ]]; then
            cd /tmp
            exec "${MODEL_ENV[@]}" "${prefix[@]}" codex exec -m "$model" --strict-config --skip-git-repo-check \
                -c model_reasoning_effort="$effort" -c sandbox_mode=read-only -c approval_policy=never \
                -c project_doc_max_bytes=0 --ephemeral --json - < "$input"
        fi
        exec "${MODEL_ENV[@]}" "${prefix[@]}" claude -p --model "$model" --effort "$effort" --tools '' \
            --strict-mcp-config --mcp-config '{"mcpServers":{}}' --no-session-persistence \
            --output-format json < "$input"
        ;;
    *) die 'Unknown bridge operation' ;;
esac
