# Repository identity, isolated Git snapshot and retained GitHub context.
# The orchestrator itself must not run checkout/reference hooks.
git() { command git -c core.hooksPath=/dev/null "$@"; }
normalize_remote() {
    local url=$1
    if [[ $url =~ ^https?://([^/]+)/(.+)$ ]]; then url=${BASH_REMATCH[1]}/${BASH_REMATCH[2]}
    elif [[ $url =~ ^ssh://([^@/]+@)?([^/]+)/(.+)$ ]]; then url=${BASH_REMATCH[2]}/${BASH_REMATCH[3]}
    elif [[ $url =~ ^[^@]+@([^:]+):(.+)$ ]]; then url=${BASH_REMATCH[1]}/${BASH_REMATCH[2]}
    else return 1; fi
    url=${url%/}; url=${url%.git}
    printf '%s\n' "${url,,}"
}
parse_repo() {
    local value=$1
    if [[ $value =~ ^([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
        HOST=${BASH_REMATCH[1]}; REPO=${BASH_REMATCH[2]}/${BASH_REMATCH[3]}
    elif [[ $value =~ ^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
        HOST=github.com; REPO=$value
    else die 'Repository must be [HOST/]OWNER/REPO'; fi
    [[ $HOST =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && $REPO != -* && $REPO != */.. && $REPO != ../* && $REPO != */. && $REPO != ./* ]] || die 'Invalid repository identity'
    HOST=${HOST,,}; REPO=${REPO,,}; GH_REPOSITORY=$HOST/$REPO
}
resolve_repository() {
    local implied='' discovered url remote normalized
    local -a matches=()
    if [[ $TARGET =~ ^https://([A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?$ ]]; then
        implied=${BASH_REMATCH[1]}/${BASH_REMATCH[2]}/${BASH_REMATCH[3]}; PR=${BASH_REMATCH[4]}
    elif [[ $TARGET =~ ^[1-9][0-9]*$ ]]; then PR=$TARGET
    else die 'Expected a positive PR number or an https://HOST/OWNER/REPO/pull/NUMBER URL'; fi
    if [[ -n $REPO_ARG ]]; then
        parse_repo "$REPO_ARG"
        [[ -z $implied || ${implied,,} == "$HOST/$REPO" ]] || die 'PR URL conflicts with --repo'
    elif [[ -n $implied ]]; then parse_repo "$implied"
    else
        discovered=$(GH_PROMPT_DISABLED=1 gh repo view --json nameWithOwner,url </dev/null) || die 'Cannot discover repository; specify --repo [HOST/]OWNER/REPO'
        url=$(jq -er '.url' <<< "$discovered")
        normalized=$(normalize_remote "$url") || die 'Unsupported GitHub repository URL; specify --repo'
        parse_repo "$normalized"
    fi
    PR_URL=https://$HOST/$REPO/pull/$PR
    while IFS= read -r remote; do
        # Read literal configured fetch URLs, before Git insteadOf rewriting.
        while IFS= read -r url; do
            normalized=$(normalize_remote "$url") || continue
            if [[ $normalized == "$HOST/$REPO" ]]; then matches+=("$remote"); break; fi
        done < <(git config --get-all "remote.$remote.url")
    done < <(git remote)
    ((${#matches[@]} == 1)) || die "Expected exactly one remote for $HOST/$REPO; found ${#matches[@]}. Check remotes and --repo."
    REMOTE=${matches[0]}
    BASE_REF=refs/pr-review/$PR/base
    HEAD_REF=refs/pr-review/$PR/head
    say "PR Review: #$PR; Repository: $HOST/$REPO; Remote: $REMOTE"
}
fetch_pr_metadata() {
    local destination=$1
    gh pr view "$PR" -R "$GH_REPOSITORY" --json number,title,body,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,author > "$destination.tmp" || die 'Repository or PR inaccessible'
    jq -e --arg pr "$PR" --arg url "${PR_URL,,}" '
      (.number | tostring) == $pr and (.url | ascii_downcase | rtrimstr("/")) == $url and
      (.headRefOid | test("^[0-9a-f]{40,64}$")) and (.baseRefOid | test("^[0-9a-f]{40,64}$")) and
      (.baseRefName | type == "string" and length > 0) and (.author.login | type == "string") and
      (.state == "OPEN" or .state == "CLOSED" or .state == "MERGED")' "$destination.tmp" >/dev/null || die 'PR metadata identity or snapshot is invalid'
    git check-ref-format "refs/heads/$(jq -r .baseRefName "$destination.tmp")" || die 'Invalid base branch'
    mv -- "$destination.tmp" "$destination"
}
api_pages() {
    local endpoint=$1 destination=$2 kind=${3:-array}
    if gh api --hostname "$HOST" --paginate --slurp "$endpoint" > "$destination.pages.tmp"; then
        if [[ $kind == actions ]]; then
            jq -e 'if type == "array" and all(.[]; (.workflow_runs | type) == "array") then [.[].workflow_runs[]] else error("invalid Actions pages") end' "$destination.pages.tmp" > "$destination.tmp" || return 1
        else
            jq -e 'if type == "array" and all(.[]; type == "array") then [ .[][] ] else error("invalid array pages") end' "$destination.pages.tmp" > "$destination.tmp" || return 1
        fi
        mv -- "$destination.tmp" "$destination"
        rm -- "$destination.pages.tmp"
    else return 1; fi
}
check_authentication() {
    gh auth status --hostname "$HOST" >/dev/null 2>&1 || die "GitHub authentication failed for $HOST"
    if provider_enabled codex; then codex login status >/dev/null 2>&1 || die 'Codex authentication failed; run codex login'; fi
    if provider_enabled claude; then claude auth status >/dev/null 2>&1 || die 'Claude authentication failed; run claude auth login'; fi
    fetch_pr_metadata "$SCRATCH/pr.json"
    VIEWER=$(gh api --hostname "$HOST" user --jq .login) || die 'Cannot identify GitHub viewer'
    [[ -n $VIEWER ]] || die 'GitHub viewer login missing'
    VIEWER_PERMISSION=$(gh repo view "$GH_REPOSITORY" --json viewerPermission --jq .viewerPermission 2>/dev/null || printf unknown)
    warn "Read access is checked; review-publication permission is unverified (repository permission: $VIEWER_PERMISSION)."
    api_pages "repos/$REPO/issues/$PR/comments" "$SCRATCH/pr-discussion.json" || die 'Cannot read PR discussion comments'
    api_pages "repos/$REPO/pulls/$PR/reviews" "$SCRATCH/pr-reviews.json" || die 'Cannot read submitted PR reviews'
    git ls-remote --exit-code "$REMOTE" "refs/pull/$PR/head" >/dev/null 2>&1 || die 'Cannot read PR ref; check remote access and gh auth setup-git for HTTPS/GH_TOKEN'
    git ls-remote --exit-code "$REMOTE" "refs/heads/$(jq -r .baseRefName "$SCRATCH/pr.json")" >/dev/null 2>&1 || die 'Cannot read base branch'
    if [[ $(jq -r .isDraft "$SCRATCH/pr.json") == true ]]; then warn 'PR is a draft'; fi
}
discover_existing_run() {
    local state_root=$1 file head common top session
    local -a matches=()
    if [[ ! -e $WORKTREE && ! -L $WORKTREE ]]; then
        for file in "$state_root"/*/manifest.json; do
            [[ -f $file ]] || continue
            session=${file%/manifest.json}
            file=$(selected_run_directory "$session")/manifest.json
            if jq -e --arg repo "$HOST/$REPO" --arg pr "$PR" --arg common "$COMMON_DIR" --arg worktree "$WORKTREE" '
                .repository==$repo and (.pr|tostring)==$pr and .git_common_dir==$common and
                .worktree==$worktree and .snapshot_update!=null' "$file" >/dev/null 2>&1; then matches+=("$session"); fi
        done
        ((${#matches[@]} > 0)) || return 0
    else
    common=$(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || die "Existing path is not a Git worktree: $WORKTREE"
    top=$(git -C "$WORKTREE" rev-parse --show-toplevel 2>/dev/null) || die "Cannot inspect existing worktree: $WORKTREE"
    [[ $common == "$COMMON_DIR" && $top == "$WORKTREE" ]] || die "Existing worktree belongs to a different checkout: $WORKTREE"
    head=$(git -C "$WORKTREE" rev-parse HEAD)
    for file in "$state_root"/*/manifest.json; do
        [[ -f $file ]] || continue
        session=${file%/manifest.json}
        file=$(selected_run_directory "$session")/manifest.json
        if jq -e --arg repo "$HOST/$REPO" --arg pr "$PR" --arg common "$COMMON_DIR" --arg worktree "$WORKTREE" --arg head "$head" '
            .format_version == 2 and .repository == $repo and (.pr | tostring) == $pr and
            .git_common_dir == $common and .worktree == $worktree and
            ((.snapshot.head // $head) == $head or .snapshot_update.to.head == $head)' "$file" >/dev/null 2>&1; then
            matches+=("$session")
        fi
    done
    fi
    case ${#matches[@]} in
        0) die "Existing worktree has no matching saved run under $state_root. Use --resume with its original run directory: $WORKTREE" ;;
        1) RESUME=${matches[0]}; say "Automatically resuming PR #$PR from $RESUME" ;;
        *) printf 'Matching saved runs:\n' >&2; printf '  %s\n' "${matches[@]}" >&2
            die 'Multiple saved runs match this worktree; select one with --resume RUN_DIRECTORY' ;;
    esac
}
locate_review_session() {
    local state_root main name
    main=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
    [[ -n $main ]] || die 'Cannot locate main checkout'
    name=${REPO##*/}
    WORKTREE=$(realpath -m -- "$(dirname -- "$main")/$name-review-pr-$PR")
    state_root=$(realpath -m -- "${XDG_STATE_HOME:-$HOME/.local/state}/pr-review/$HOST/$REPO")
    if [[ -z $RESUME ]]; then discover_existing_run "$state_root"; fi
}
initialize_run() {
    local state_root main file publication_dir
    mkdir -p -- "$COMMON_DIR/pr-review-locks"
    exec {LOCK_FD}>"$COMMON_DIR/pr-review-locks/$PR.lock"
    flock -n "$LOCK_FD" || die "Another run holds the lock for PR #$PR"
    locate_review_session
    main=$(git worktree list --porcelain | sed -n '1s/^worktree //p')
    state_root=$(realpath -m -- "${XDG_STATE_HOME:-$HOME/.local/state}/pr-review/$HOST/$REPO")
    if [[ -n $RESUME ]]; then
        RUN_DIR=$(realpath -e -- "$RESUME") || die 'Resume directory does not exist'
        [[ -f $RUN_DIR/manifest.json ]] || die 'Resume manifest missing'
        SESSION_DIR=$(jq -r '.session_directory // empty' "$RUN_DIR/manifest.json")
        [[ -n $SESSION_DIR ]] || SESSION_DIR=$RUN_DIR
        RUN_DIR=$(selected_run_directory "$SESSION_DIR") || die 'Saved review attempt is missing'
        # A crash after committing the pointer but before removing the pending
        # link has already installed a complete generation. Finish that cleanup.
        if [[ -L $SESSION_DIR/.current-review && -L $SESSION_DIR/.pending-review &&
              $(realpath -e "$SESSION_DIR/.current-review") == "$RUN_DIR" ]]; then
            rm -f -- "$SESSION_DIR/.pending-review"
        fi
        [[ $(jq -r .format_version "$RUN_DIR/manifest.json") == 2 ]] || die 'Legacy run remains readable but cannot resume discussion-aware publication; start a new run after moving its worktree.'
        jq -e --arg repo "$HOST/$REPO" --arg pr "$PR" --arg common "$COMMON_DIR" '
          .format_version == 2 and .repository == $repo and (.pr | tostring) == $pr and .git_common_dir == $common' "$RUN_DIR/manifest.json" >/dev/null || die 'Resume repository or PR identity does not match'
        WORKTREE=$(jq -er .worktree "$RUN_DIR/manifest.json")
        RUN_ID=$(jq -er .run_id "$RUN_DIR/manifest.json")
        for publication_dir in "$RUN_DIR" "$(current_run_directory)"; do
            if [[ -f $publication_dir/publication/drafts.json ]] &&
                jq -e 'any(.batches[]?.actions[]?; .status=="submitting" or .status=="ambiguous")' "$publication_dir/publication/drafts.json" >/dev/null; then
                die 'Resolve uncertain publication in the Auto-review publication menu before running or replacing this review'
            fi
            if [[ $publication_dir != "$RUN_DIR" && -f $publication_dir/publication/actions.json ]] &&
                jq -e 'any(.[]; .status!="draft" and .status!="published")' "$publication_dir/publication/actions.json" >/dev/null; then
                die 'Resolve uncertain publication in the previous review before resuming its replacement; all drafts and receipts are retained'
            fi
        done
        if jq -e '.publication_reset.status=="pending"' "$RUN_DIR/manifest.json" >/dev/null; then finish_publication_reset; fi
        if jq -e '.snapshot_update!=null' "$RUN_DIR/manifest.json" >/dev/null; then
            # An interrupted cleanup can leave these absent. Authentication
            # still runs first, then prepare_worktree finishes the saved update.
            mkdir -p -- "$RUN_DIR/logs"
            if [[ ! -f $RUN_DIR/logs/commands.json ]]; then printf '[]\n' > "$RUN_DIR/logs/commands.json"; fi
        fi
    else
        case $state_root/ in "$main/"*|"$WORKTREE/"*|"$(git rev-parse --show-toplevel)/"*) die 'Artifacts must be outside the reviewed repository; set XDG_STATE_HOME' ;; esac
        mkdir -p -- "$state_root"
        RUN_DIR=$(mktemp -d "$state_root/pr-$PR-$(date -u +%Y%m%dT%H%M%S)-XXXXXX")
        RUN_ID=${RUN_DIR##*/}
        SESSION_DIR=$RUN_DIR
        mkdir -p -- "$RUN_DIR"/{context,prompts,reviews,publication,logs,discussion}
        jq -n --arg repository "$HOST/$REPO" --argjson pr "$PR" --arg url "$PR_URL" --arg common "$COMMON_DIR" --arg worktree "$WORKTREE" --arg id "$RUN_ID" --arg time "$(now)" \
            '{format_version:2,repository:$repository,pr:$pr,url:$url,git_common_dir:$common,worktree:$worktree,run_id:$id,created_at:$time,status:"running",phases:{},publication:{status:"not_published"},warnings:[],generated_files:[],removed_environment:{}}' > "$RUN_DIR/manifest.json.tmp"
        mv -- "$RUN_DIR/manifest.json.tmp" "$RUN_DIR/manifest.json"
        printf '[]\n' > "$RUN_DIR/logs/commands.json"
        printf '[]\n' > "$RUN_DIR/publication/actions.json"
    fi
    # Keep the original provenance before this invocation updates tools/models
    # or renders prompts. Used only to verify runs predating per-stage inputs.
    cp -- "$RUN_DIR/manifest.json" "$SCRATCH/resume-manifest.json"
    mkdir -p -- "$SCRATCH/resume-prompts"
    for file in "$RUN_DIR"/prompts/*-prompt.txt; do
        [[ ! -f $file ]] || cp -- "$file" "$SCRATCH/resume-prompts/"
    done
    # Configuration belongs to its generation. Fork before recording a change.
    prepare_analysis_kind
    local saved_configuration
    saved_configuration=$(python3 "$ROOT/lib/review-settings.py" <<< "$(jq -cn --arg file "$RUN_DIR/manifest.json" '{manifest:$file}')")
    if [[ $(jq -cS . <<< "$saved_configuration") != "$(jq -cS . <<< "$RUN_CONFIGURATION")" ]] &&
        jq -e '.phases.synthesis.status=="completed" or .phases.followup.status=="completed"' "$RUN_DIR/manifest.json" >/dev/null; then begin_review_attempt; fi
    manifest '.configuration=$configuration | .tools = {git:$git,gh:$gh,codex:$codex,claude:$claude} | .models = {codex:$cm,claude:$am,claude_effort:$ce,codex_effort:$xe,claude_permission_mode:$mode} | .viewer = $viewer | .viewer_permission = $permission | .status = "running"' \
        --arg git "$GIT_VERSION" --arg gh "$GH_VERSION" --arg codex "$CODEX_VERSION" --arg claude "$CLAUDE_VERSION" --argjson configuration "$RUN_CONFIGURATION" --arg ce "$CLAUDE_EFFORT" --arg xe "$CODEX_EFFORT" --arg cm "$CODEX_MODEL" --arg am "$CLAUDE_MODEL" --arg mode "$PERMISSION_MODE" --arg viewer "$VIEWER" --arg permission "$VIEWER_PERMISSION"
    say "Artifacts: $RUN_DIR"
}
prepare_worktree() {
    local attempt fetched_head fetched_base base_source matched=false old_head old_merge
    if jq -e '.snapshot_update!=null' "$RUN_DIR/manifest.json" >/dev/null; then finish_snapshot_update; fi
    for attempt in 1 2; do
        BASE_BRANCH=$(jq -r .baseRefName "$SCRATCH/pr.json")
        base_source=refs/heads/$BASE_BRANCH
        # Closed PR metadata can retain a historical base while the branch moves
        # on. Fetch that recorded commit, preserving historical review support.
        if [[ $(jq -r .state "$SCRATCH/pr.json") != OPEN ]]; then
            base_source=$(jq -r .baseRefOid "$SCRATCH/pr.json")
        fi
        git fetch --no-tags --refmap= --no-write-fetch-head "$REMOTE" "+refs/pull/$PR/head:$HEAD_REF" "+$base_source:$BASE_REF" || die 'Cannot fetch PR and base'
        # Metadata after fetch closes normal setup races without assuming base is stationary.
        fetch_pr_metadata "$SCRATCH/pr.json"
        fetched_head=$(git rev-parse "$HEAD_REF"); fetched_base=$(git rev-parse "$BASE_REF")
        if [[ $fetched_head == "$(jq -r .headRefOid "$SCRATCH/pr.json")" && $fetched_base == "$(jq -r .baseRefOid "$SCRATCH/pr.json")" && $BASE_BRANCH == "$(jq -r .baseRefName "$SCRATCH/pr.json")" ]]; then matched=true; break; fi
        warn "PR refs changed during setup (attempt $attempt); refreshing snapshot"
    done
    $matched || die 'Could not establish a consistent snapshot after retry'
    HEAD_SHA=$fetched_head; BASE_SHA=$fetched_base
    MERGE_BASE=$(git merge-base "$HEAD_SHA" "$BASE_SHA") || die 'Cannot find merge-base (fetch full history if checkout is shallow)'
    if [[ -n $RESUME ]]; then
        old_head=$(jq -r '.snapshot.head // empty' "$RUN_DIR/manifest.json")
        old_merge=$(jq -r '.snapshot.merge_base // empty' "$RUN_DIR/manifest.json")
        if [[ -n $old_head && ( $old_head != "$HEAD_SHA" || $old_merge != "$MERGE_BASE" ) ]]; then
            update_review_snapshot
        fi
        if [[ ! -e $WORKTREE && $(jq -r '.phases.setup.status // empty' "$RUN_DIR/manifest.json") != completed ]]; then
            git worktree add --detach "$WORKTREE" "$HEAD_SHA" || die 'Cannot resume worktree preparation'
        fi
        [[ -d $WORKTREE && $(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir) == "$COMMON_DIR" ]] || die 'Resume worktree missing or belongs to a different repository'
        verify_snapshot
        local extras
        extras=$(comm -23 <(git -C "$WORKTREE" status --porcelain=v1 --untracked-files=all --ignored | sort) <(jq -r '.generated_files[]' "$RUN_DIR/manifest.json" | sort))
        [[ -z $extras ]] || die "Resume worktree has unrecorded untracked/ignored files; inspect them before resuming: $extras"
    else
        git worktree add --detach "$WORKTREE" "$HEAD_SHA" || die 'Cannot create review worktree'
        verify_snapshot
    fi
    manifest '.snapshot = {head:$head,base:$base,merge_base:$merge,base_branch:$branch} | .phases.setup = {status:"completed",exit_status:0}' --arg head "$HEAD_SHA" --arg base "$BASE_SHA" --arg merge "$MERGE_BASE" --arg branch "$BASE_BRANCH"
    say "Base: $BASE_BRANCH ($BASE_SHA); Head: $HEAD_SHA; Merge-base: $MERGE_BASE"
}
verify_snapshot() {
    if [[ $(git -C "$WORKTREE" rev-parse HEAD) != "$HEAD_SHA" ]] ||
        ! git -C "$WORKTREE" diff --quiet HEAD -- || ! git -C "$WORKTREE" diff --cached --quiet -- ||
        [[ $(git rev-parse "$HEAD_REF") != "$HEAD_SHA" || $(git rev-parse "$BASE_REF") != "$BASE_SHA" ]]; then
        manifest '.integrity_failed = true'
        die 'Worktree HEAD, tracked source, index or snapshot refs changed; publication blocked'
    fi
}
check_agent_config_changes() {
    local path found=false answer
    : > "$RUN_DIR/context/agent-config-changes.txt.tmp"
    while IFS= read -r -d '' path; do
        case /$path/ in
            */AGENTS.md/|*/AGENTS.override.md/|*/CLAUDE.md/|*/CLAUDE.local.md/|*/.claude/*|*/.codex/*|*/.mcp.json/)
                printf '%q\n' "$path" >> "$RUN_DIR/context/agent-config-changes.txt.tmp"; found=true ;;
        esac
    done < <(git -C "$WORKTREE" diff --name-only --no-renames -z "$MERGE_BASE" "$HEAD_SHA")
    mv -- "$RUN_DIR/context/agent-config-changes.txt.tmp" "$RUN_DIR/context/agent-config-changes.txt"
    if $found; then
        warn 'This PR changes agent configuration loaded by the models:'
        cat -- "$RUN_DIR/context/agent-config-changes.txt"
        if [[ $(jq -r '.agent_config_confirmed // false' "$RUN_DIR/manifest.json") != true ]]; then
            if [[ $TRUSTED_SNAPSHOT != "$HEAD_SHA:$MERGE_BASE" ]]; then
                if [[ ${PR_REVIEW_API:-} == 1 ]]; then
                    jq -cn --arg snapshot "$HEAD_SHA:$MERGE_BASE" --rawfile paths "$RUN_DIR/context/agent-config-changes.txt" \
                        '{confirmation_required:"agent_configuration",snapshot:$snapshot,paths:$paths}'
                fi
                $INTERACTIVE || die 'Agent configuration changes require interactive confirmation'
                read -r -p 'Trust these configuration changes and run models? Type yes: ' answer || answer=
                [[ $answer == yes ]] || die 'Agent configuration was not confirmed'
            fi
            manifest '.agent_config_confirmed = true'
        fi
    fi
}
context_warning() {
    warn "$1"
    manifest '.warnings += [$warning] | .warnings |= unique' --arg warning "$1"
}
collect_pr_context() {
    local filename endpoint kind
    if [[ $(jq -r '.phases.context.status // empty' "$RUN_DIR/manifest.json") != completed ]] ||
        [[ $(context_hash) != "$(jq -r '.phases.context.output_hash // empty' "$RUN_DIR/manifest.json")" ]]; then
        cp -- "$SCRATCH/pr.json" "$RUN_DIR/context/pr.json.tmp"; mv -- "$RUN_DIR/context/pr.json.tmp" "$RUN_DIR/context/pr.json"
        cp -- "$SCRATCH/pr-discussion.json" "$RUN_DIR/context/pr-discussion.json"
        cp -- "$SCRATCH/pr-reviews.json" "$RUN_DIR/context/pr-reviews.json"
        for filename in pr-inline-comments ci-actions-runs ci-statuses; do
            kind=array
            case $filename in
                pr-inline-comments) endpoint=repos/$REPO/pulls/$PR/comments ;;
                ci-actions-runs) endpoint="repos/$REPO/actions/runs?head_sha=$HEAD_SHA"; kind=actions ;;
                ci-statuses) endpoint=repos/$REPO/commits/$HEAD_SHA/statuses ;;
            esac
            if ! api_pages "$endpoint" "$RUN_DIR/context/$filename.json" "$kind" 2> "$RUN_DIR/logs/$filename.stderr.log"; then
                printf 'null\n' > "$RUN_DIR/context/$filename.json.tmp"; mv -- "$RUN_DIR/context/$filename.json.tmp" "$RUN_DIR/context/$filename.json"
                context_warning "$filename unavailable; context is incomplete."
            fi
        done
        if gh pr view "$PR" -R "$GH_REPOSITORY" --comments > "$RUN_DIR/context/pr-comments.txt.tmp" 2> "$RUN_DIR/logs/pr-view.stderr.log"; then
            mv -- "$RUN_DIR/context/pr-comments.txt.tmp" "$RUN_DIR/context/pr-comments.txt"
        else context_warning 'Rendered PR comments unavailable; see structured discussion.'; fi
        jq -r '"# \(.title)\n\n\(.body)\n\n\(.url)"' "$RUN_DIR/context/pr.json" > "$RUN_DIR/context/pr-view.txt"
        git -C "$WORKTREE" diff --no-ext-diff --no-textconv "$MERGE_BASE" "$HEAD_SHA" -- > "$RUN_DIR/context/pr.diff.tmp"
        mv -- "$RUN_DIR/context/pr.diff.tmp" "$RUN_DIR/context/pr.diff"
        git -C "$WORKTREE" diff --stat "$MERGE_BASE" "$HEAD_SHA" -- > "$RUN_DIR/context/diffstat.txt"
        git -C "$WORKTREE" diff --name-only -z "$MERGE_BASE" "$HEAD_SHA" -- > "$RUN_DIR/context/files.zlist"
        manifest '.phases.context = {status:"completed",exit_status:0,output_hash:$hash}' --arg hash "$(context_hash)"
    fi
    jq -n --slurpfile discussion "$RUN_DIR/context/pr-discussion.json" --slurpfile reviews "$RUN_DIR/context/pr-reviews.json" --slurpfile inline "$RUN_DIR/context/pr-inline-comments.json" \
        '"Discussion: \($discussion[0]|length); reviews: \($reviews[0]|length); inline comments: \(if $inline[0] == null then "unavailable" else ($inline[0]|length) end)"' -r
    # Keep the newest legacy status per context; old failures are not current CI.
    jq -nr --slurpfile actions "$RUN_DIR/context/ci-actions-runs.json" --slurpfile statuses "$RUN_DIR/context/ci-statuses.json" '
      [($actions[0] // [])[] | if .status != "completed" then "pending" elif (.conclusion == "success" or .conclusion == "neutral" or .conclusion == "skipped") then "passed" else "failed" end] +
      [($statuses[0] // [] | sort_by(.context, .created_at) | group_by(.context) | .[] | last) | if .state == "success" then "passed" elif .state == "pending" then "pending" else "failed" end] |
      if length == 0 then "CI: no Actions runs or statuses found (unavailable sources are reported separately)." else "CI: \(map(select(. == "passed"))|length) passed, \(map(select(. == "failed"))|length) failed, \(map(select(. == "pending"))|length) pending." end'
    say 'CI coverage excludes check runs from third-party GitHub Apps.'
    cat -- "$RUN_DIR/context/diffstat.txt"
}
context_hash() {
    (cd -- "$RUN_DIR/context" && find . -maxdepth 1 -type f ! -name '*.tmp' -print0 | sort -z | xargs -0 -r sha256sum) | sha256sum | cut -d ' ' -f1
}
