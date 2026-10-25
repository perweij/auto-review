# Shared lifecycle and manifest operations. Sourced by bin/pr-review.
RUN_DIR='' WORKTREE='' ACTIVE_STAGE='' MODEL_PID='' HEARTBEAT_PID='' SCRATCH=''
NO_PUBLISH=false RESUME='' REPO_ARG='' TARGET='' INTERACTIVE=false
TRUSTED_SNAPSHOT=''
SYNTHESIS_TESTS=focused
REVIEWERS=claude,codex PROCESSING_PROVIDER=claude
CLAUDE_EFFORT=high CODEX_EFFORT=high
OPTION_OVERRIDES='{}' RUN_CONFIGURATION='{}'
CODEX_VERSION=disabled CLAUDE_VERSION=disabled
CODEX_MODEL=gpt-6-astra CLAUDE_MODEL=fable
PERMISSION_MODE=${PR_REVIEW_CLAUDE_PERMISSION_MODE:-auto}
CLAUDE_DENY=(Edit Write NotebookEdit 'Bash(git push:*)' 'Bash(git commit:*)'
    'Bash(git checkout:*)' 'Bash(git switch:*)' 'Bash(git reset:*)'
    'Bash(git restore:*)' 'Bash(git stash:*)' 'Bash(gh:*)')

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
hash_file() { sha256sum -- "$1" | cut -d ' ' -f1; }
usage() {
    cat <<'EOF'
Usage: pr-review <PR_NUMBER | PR_URL> [options]
       pr-review api             Version 1 JSON request on stdin; NDJSON responses
  -R, --repo [HOST/]OWNER/REPO   Override repository discovery
  --no-publish                  Never write to GitHub
  --resume RUN_DIRECTORY        Select a saved run explicitly (otherwise auto-match)
  --codex-model MODEL           Default: gpt-6-astra (effort high)
  --claude-model MODEL          Default: fable (effort high)
  --reviewers PROVIDERS        claude,codex (default), claude, or codex
  --processing-provider NAME   claude (default) or codex; fixed in single mode
  --claude-effort LEVEL         Default: high
  --codex-effort LEVEL          Default: high
  --synthesis-tests MODE        focused (default), or full: permit one suite attempt
  --trust-agent-config HEAD:MERGE_BASE  Explicit trust for this exact snapshot
  -h, --help
Independent reviews always use focused checks; full-suite permission is reserved
for synthesis (or the structured review when only one provider is enabled).
Environment: PR_REVIEW_CLAUDE_PERMISSION_MODE (auto/dontAsk/manual/plan),
  PR_REVIEW_MODEL_TIMEOUT (optional seconds), PR_REVIEW_CLAUDE_BUDGET
  (optional USD), PR_REVIEW_AUTH_TIMEOUT (seconds, default 60),
  PR_REVIEW_CODEX_PROBE_MODEL (default gpt-6-luna), XDG_STATE_HOME, VISUAL/EDITOR.
EOF
}
parse_args() {
    while (($#)); do
        case $1 in
            -h|--help) usage; exit 0 ;;
            --no-publish) NO_PUBLISH=true; shift ;;
            -R|--repo|--resume|--codex-model|--claude-model|--trust-agent-config|--synthesis-tests|--reviewers|--processing-provider|--claude-effort|--codex-effort)
                if (($# < 2)) || [[ -z $2 || $2 == -* ]]; then die "Missing value for $1"; fi
                case $1 in
                    -R|--repo) REPO_ARG=$2 ;;
                    --resume) RESUME=$2 ;;
                    --codex-model) CODEX_MODEL=$2 ;;
                    --claude-model) CLAUDE_MODEL=$2 ;;
                    --trust-agent-config) TRUSTED_SNAPSHOT=$2 ;;
                    --synthesis-tests) SYNTHESIS_TESTS=$2 ;;
                    --reviewers) REVIEWERS=$2 ;;
                    --processing-provider) PROCESSING_PROVIDER=$2 ;;
                    --claude-effort) CLAUDE_EFFORT=$2 ;;
                    --codex-effort) CODEX_EFFORT=$2 ;;
                esac
                case $1 in
                    --reviewers) OPTION_OVERRIDES=$(jq -c --arg v "$2" '.reviewers=($v|split(","))' <<< "$OPTION_OVERRIDES") ;;
                    --*-model|--*-effort|--processing-provider|--synthesis-tests)
                        local key=${1#--}; key=${key//-/_}
                        OPTION_OVERRIDES=$(jq -c --arg k "$key" --arg v "$2" '.[$k]=$v' <<< "$OPTION_OVERRIDES") ;;
                esac
                shift 2 ;;
            -*) die "Unknown option: $1" ;;
            *) [[ -z $TARGET ]] || die 'Specify exactly one PR'; TARGET=$1; shift ;;
        esac
    done
    [[ -n $TARGET ]] || die 'A PR number or URL is required; see --help'
    if [[ -t 0 ]]; then INTERACTIVE=true; else NO_PUBLISH=true; fi
    case $PERMISSION_MODE in auto|dontAsk|manual|plan) ;; *) die 'Unsupported Claude permission mode' ;; esac
    case $SYNTHESIS_TESTS in focused|full) ;; *) die '--synthesis-tests must be focused or full' ;; esac
    [[ -z ${PR_REVIEW_MODEL_TIMEOUT:-} || ${PR_REVIEW_MODEL_TIMEOUT} =~ ^[1-9][0-9]*$ ]] || die 'PR_REVIEW_MODEL_TIMEOUT must be positive integer seconds'
    [[ ${PR_REVIEW_AUTH_TIMEOUT:-60} =~ ^[1-9][0-9]*$ ]] || die 'PR_REVIEW_AUTH_TIMEOUT must be positive integer seconds'
    [[ -z ${PR_REVIEW_CLAUDE_BUDGET:-} || ${PR_REVIEW_CLAUDE_BUDGET} =~ ^[0-9]+([.][0-9]+)?$ ]] || die 'PR_REVIEW_CLAUDE_BUDGET must be a USD amount'
}
manifest() {
    local filter=$1; shift
    jq "$@" "$filter | .updated_at = \$updated" --arg updated "$(now)" "$RUN_DIR/manifest.json" > "$RUN_DIR/manifest.json.tmp"
    mv -- "$RUN_DIR/manifest.json.tmp" "$RUN_DIR/manifest.json"
}
stop_model_group() {
    local signal=$1 deadline
    [[ -n $MODEL_PID ]] || return 0
    kill -s "$signal" -- "-$MODEL_PID" 2>/dev/null || true
    deadline=$((SECONDS+2))
    while kill -0 -- "-$MODEL_PID" 2>/dev/null && ((SECONDS<deadline)); do sleep 0.1; done
    if kill -0 -- "-$MODEL_PID" 2>/dev/null; then
        warn 'Model did not stop promptly; sending TERM.'
        kill -TERM -- "-$MODEL_PID" 2>/dev/null || true
        deadline=$((SECONDS+2))
        while kill -0 -- "-$MODEL_PID" 2>/dev/null && ((SECONDS<deadline)); do sleep 0.1; done
    fi
    if kill -0 -- "-$MODEL_PID" 2>/dev/null; then
        warn 'Stopping remaining model processes with KILL.'
        kill -KILL -- "-$MODEL_PID" 2>/dev/null || true
    fi
    deadline=$((SECONDS+1))
    while kill -0 "$MODEL_PID" 2>/dev/null && ((SECONDS<deadline)); do sleep 0.1; done
    # Do not re-enter an unbounded wait if a child is stuck in kernel I/O.
    if ! kill -0 "$MODEL_PID" 2>/dev/null; then wait "$MODEL_PID" 2>/dev/null || true
    else warn "Model PID $MODEL_PID has not exited after KILL; inspect it from the host."; fi
    MODEL_PID=
}
on_signal() {
    local signal=$1 code=$2
    trap '' INT TERM
    if [[ -n $MODEL_PID ]]; then
        warn 'Stopping the active model; escalating signals if necessary.'
        stop_model_group "$signal"
    fi
    if [[ -n $ACTIVE_STAGE && -f $RUN_DIR/manifest.json ]]; then
        check_model_integrity
        manifest '.phases[$stage].status = "interrupted" | .phases[$stage].exit_status = $code | .phases[$stage].finished_at = $time' --arg stage "$ACTIVE_STAGE" --argjson code "$code" --arg time "$(now)"
        ACTIVE_STAGE=
    fi
    exit "$code"
}
on_exit() {
    local rc=$?
    trap - EXIT
    if [[ -n $HEARTBEAT_PID ]]; then kill -- "-$HEARTBEAT_PID" 2>/dev/null || true; wait "$HEARTBEAT_PID" 2>/dev/null || true; fi
    if [[ -n $SCRATCH ]]; then rm -rf -- "$SCRATCH"; fi
    if [[ -n $RUN_DIR && -f $RUN_DIR/manifest.json ]]; then
        if ((rc != 0)); then
            if [[ -n $ACTIVE_STAGE ]]; then
                manifest '.phases[$stage].status = "failed" | .phases[$stage].exit_status = $code' --arg stage "$ACTIVE_STAGE" --argjson code "$rc" || true
            fi
            manifest '.status = "failed"' || true
        fi
        printf '\nPR:        %s\nWorktree:  %s\nArtifacts: %s\nReview:    %s/reviews/synthesis.md\nStatus:    ' "${PR_URL:-unknown}" "$WORKTREE" "$RUN_DIR" "$RUN_DIR"
        if ((rc != 0)) && [[ $(jq -r '.publication.published_actions // 0' "$RUN_DIR/manifest.json") != 0 ]]; then say 'Partially published (remaining work stopped)'
        elif ((rc != 0)); then say Failed
        elif [[ $(jq -r '.publication.status' "$RUN_DIR/manifest.json") == paused ]]; then say 'Publication paused'
        elif [[ $(jq -r '.publication.status' "$RUN_DIR/manifest.json") == published ]]; then say Published
        elif [[ $(jq -r '.publication.published_actions // 0' "$RUN_DIR/manifest.json") != 0 ]]; then say 'Partially published (remaining actions not posted)'
        else say 'Not published'; fi
        if [[ -n ${SESSION_DIR:-} && -L $SESSION_DIR/.pending-review ]]; then
            printf 'Previous review: %s/reviews/synthesis.md (replacement not installed)\n' "$(current_run_directory)"
            printf 'Resume session:  %s\n' "$SESSION_DIR"
        fi
        if [[ -f $RUN_DIR/publication/actions.json ]]; then
            jq -r '.[]|select(.status=="published")|"Posted:    \(.id) \(.kind) \(.result.html_url // "(see saved API response)")"' "$RUN_DIR/publication/actions.json"
        fi
    fi
    exit "$rc"
}
require_help() {
    local help=$1; shift
    local flag
    for flag in "$@"; do [[ $help == *"$flag"* ]] || die "Installed CLI lacks required capability: $flag"; done
}
check_dependencies() {
    local tool help
    for tool in git gh python3 jq flock timeout sha256sum realpath readlink; do command -v "$tool" >/dev/null || die "Missing required executable: $tool"; done
    GIT_VERSION=$(git --version); GH_VERSION=$(gh --version | head -n1)
    help=$(gh api --help); require_help "$help" --hostname --paginate --slurp --method --field --raw-field
    help=$(gh pr view --help); require_help "$help" --json --comments --repo
    help=$(gh auth status --help); require_help "$help" --hostname
    help=$(git worktree -h 2>&1 || true); require_help "$help" --detach
    help=$(git fetch -h 2>&1 || true); require_help "$help" --refmap write-fetch-head --no-tags
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 'Run inside a usable Git checkout'
    COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir)
    SCRATCH=$(mktemp -d)
}

provider_enabled() { [[ ,$REVIEWERS, == *,$1,* ]]; }
dual_review() { [[ $REVIEWERS == claude,codex ]]; }
resolve_review_settings() {
    local request session selected file=''
    locate_review_session
    if [[ -n $RESUME ]]; then
        selected=$(realpath -e -- "$RESUME") || die 'Resume directory does not exist'
        [[ -f $selected/manifest.json ]] || die 'Resume manifest missing'
        session=$(jq -r '.session_directory // empty' "$selected/manifest.json")
        selected=$(selected_run_directory "${session:-$selected}")
        file=$selected/manifest.json
    fi
    request=$(jq -cn --arg file "$file" --argjson overrides "$OPTION_OVERRIDES" '{manifest:$file,overrides:$overrides}')
    RUN_CONFIGURATION=$(printf '%s\n' "$request" | python3 "$ROOT/lib/review-settings.py") || die 'Cannot resolve review settings'
    REVIEWERS=$(jq -r '.reviewers|join(",")' <<< "$RUN_CONFIGURATION")
    PROCESSING_PROVIDER=$(jq -r .processing_provider <<< "$RUN_CONFIGURATION")
    CLAUDE_MODEL=$(jq -r .claude_model <<< "$RUN_CONFIGURATION")
    CODEX_MODEL=$(jq -r .codex_model <<< "$RUN_CONFIGURATION")
    CLAUDE_EFFORT=$(jq -r .claude_effort <<< "$RUN_CONFIGURATION")
    CODEX_EFFORT=$(jq -r .codex_effort <<< "$RUN_CONFIGURATION")
    SYNTHESIS_TESTS=$(jq -r .synthesis_tests <<< "$RUN_CONFIGURATION")
}
check_model_dependencies() {
    local help
    if provider_enabled codex; then
        command -v codex >/dev/null || die 'Missing required executable: codex'
        CODEX_VERSION=$(codex --version)
        help=$(codex exec review --help)
        require_help "$help" --base --output-last-message --json --ephemeral --model --config --strict-config
        help=$(codex exec --help); require_help "$help" --skip-git-repo-check --output-last-message --json --ephemeral
        if [[ $PROCESSING_PROVIDER == codex ]]; then require_help "$help" --output-schema; fi
        help=$(codex login --help); require_help "$help" status
    fi
    if provider_enabled claude; then
        command -v claude >/dev/null || die 'Missing required executable: claude'
        CLAUDE_VERSION=$(claude --version)
        help=$(claude --help)
        require_help "$help" --print --model --effort --permission-mode --output-format stream-json --verbose --include-partial-messages --no-session-persistence --disallowed-tools --add-dir --json-schema --append-system-prompt
        require_help "$help" --tools --disable-slash-commands --strict-mcp-config --system-prompt --setting-sources
        if [[ -n ${PR_REVIEW_CLAUDE_BUDGET:-} ]]; then require_help "$help" --max-budget-usd; fi
        help=$(claude auth --help); require_help "$help" status
    fi
    say "$GIT_VERSION; $GH_VERSION; $CODEX_VERSION; $CLAUDE_VERSION"
}
