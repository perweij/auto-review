# Model lifecycle: explicit inputs, filtered environment, liveness, integrity.
is_auth_probe() { [[ $ACTIVE_STAGE == claude_auth_probe || $ACTIVE_STAGE == codex_auth_probe ]]; }

report_probe_failure() {
    local cli=$1 rc=$2 events=$3 stderr=$4 reason
    if ((rc == 124 || rc == 137)); then reason=timeout
    else
        # Classify locally; never replay arbitrary provider output or credentials.
        reason=$(jq -Rsr 'ascii_downcase |
          if test("oauth session expired|unauthorized|authentication|failed to authenticate|invalid.{0,20}(api.?key|token)|token.{0,20}expired|401") then "authentication"
          elif test("quota|rate.limit|usage.limit|insufficient.credit|429") then "quota"
          elif test("model.{0,80}(not found|not available|unavailable|not supported|does not exist|access)|unsupported.model") then "model_access"
          elif test("connection|network|dns|timed out|unable to connect") then "network"
          else "unverified" end' "$events" "$stderr")
    fi
    manifest '.phases[$stage].failure_reason = $reason' --arg stage "$ACTIVE_STAGE" --arg reason "$reason"
    case $reason in
        authentication)
            if [[ $cli == claude ]]; then warn "Live authentication failed. Run 'claude auth login' and retry."
            else warn "Live authentication failed. Run 'codex login' and retry."; fi ;;
        quota) warn 'Live probe blocked by a usage or rate limit; authentication could not be confirmed.' ;;
        model_access) warn 'Probe model unavailable or not permitted; authentication could not be confirmed.'
            if [[ $cli == codex ]]; then warn 'Set PR_REVIEW_CODEX_PROBE_MODEL to an inexpensive model available to your account.'; fi ;;
        timeout) warn 'Live probe timed out; authentication could not be confirmed. Adjust PR_REVIEW_AUTH_TIMEOUT if needed.' ;;
        network) warn 'Live probe could not reach the service; authentication could not be confirmed.' ;;
        *) warn 'Live probe did not return a successful diagnostic response; authentication could not be confirmed.' ;;
    esac
}

check_live_authentication() {
    local cli model events stderr response valid
    local FINGERPRINT='' PR_REVIEW_MODEL_TIMEOUT=${PR_REVIEW_AUTH_TIMEOUT:-60}
    local prompt='Reply with exactly PR_REVIEW_AUTH_OK. Do not use tools or inspect files.'
    mkdir -p -- "$SCRATCH/auth-probe"
    for cli in claude codex; do
        ACTIVE_STAGE=${cli}_auth_probe
        events=$RUN_DIR/logs/$ACTIVE_STAGE.jsonl
        stderr=$RUN_DIR/logs/$ACTIVE_STAGE.stderr.log
        response=$RUN_DIR/logs/$ACTIVE_STAGE.response.txt
        rm -f -- "$response"
        if [[ $cli == claude ]]; then
            model=haiku
            say "Checking live Claude access ($model)..."
            run_model claude /dev/null "$events" "$stderr" \
                claude -p --model "$model" --effort low --permission-mode dontAsk \
                --no-session-persistence --output-format json --tools '' \
                --disable-slash-commands --strict-mcp-config --setting-sources user \
                --system-prompt 'You are an authentication diagnostic. Follow the requested output exactly.' -- "$prompt"
            valid=false
            if jq -se '[.[] | select(type=="object" and .type=="result")][-1] |
                .subtype=="success" and .is_error==false and .result=="PR_REVIEW_AUTH_OK"' "$events" >/dev/null 2>&1; then valid=true; fi
        else
            model=${PR_REVIEW_CODEX_PROBE_MODEL:-gpt-6-luna}
            say "Checking live Codex access ($model)..."
            run_model codex /dev/null "$events" "$stderr" \
                codex exec -m "$model" --strict-config --skip-git-repo-check \
                -c model_reasoning_effort=low -c sandbox_mode=read-only -c approval_policy=never \
                -c project_doc_max_bytes=0 --ephemeral --json -o "$response" -- "$prompt"
            valid=false
            if [[ -f $response && $(cat "$response") == PR_REVIEW_AUTH_OK ]] &&
                jq -se 'any(.[]; .type=="turn.completed") and
                    all(.[]; .type!="turn.failed" and .type!="error")' "$events" >/dev/null 2>&1; then valid=true; fi
        fi
        if ! $valid; then
            manifest '.phases[$stage].status = "failed"' --arg stage "$ACTIVE_STAGE"
            report_model_failure "$cli" 0 "$events" "$stderr" "$model"
            ACTIVE_STAGE=
            die 'Live authentication preflight failed; review setup stopped'
        fi
        complete_phase "$events"
        say "Live $cli access confirmed ($model)."
    done
}

model_environment() {
    local cli=$1 name upper
    local -a removed=()
    MODEL_ENV=(env)
    while IFS= read -r name; do
        upper=${name^^}
        case $upper in
            GIT_CONFIG_COUNT|GIT_CONFIG_PARAMETERS|GIT_CONFIG_KEY_*|GIT_CONFIG_VALUE_*)
                # These overrides are one unit. Removing just KEY_n (via the
                # credential pattern below) leaves Git unable to read any repo.
                # Values/parameters may also carry credential-helper secrets.
                MODEL_ENV+=(-u "$name"); removed+=("$name"); continue ;;
            *TOKEN*|*SECRET*|*PASSWORD*|*PASSWD*|*CREDENTIAL*|*_KEY|*_KEY_*)
                case $cli:$name in
                    claude:ANTHROPIC_API_KEY|claude:CLAUDE_CODE_OAUTH_TOKEN|codex:OPENAI_API_KEY|codex:CODEX_API_KEY) continue ;;
                esac
                MODEL_ENV+=(-u "$name"); removed+=("$name") ;;
        esac
    done < <(compgen -e)
    manifest '.removed_environment[$cli] = $names' --arg cli "$cli" --argjson names "$(printf '%s\n' "${removed[@]}" | jq -Rsc 'split("\n") | map(select(length > 0)) | sort')"
}
phase_fingerprint() {
    local stage=$1 model version
    if [[ $stage == codex ]]; then model=$CODEX_MODEL; version=$CODEX_VERSION; else model=$CLAUDE_MODEL; version=$CLAUDE_VERSION; fi
    {
        printf '%s\n' "$stage" "$HEAD_SHA" "$MERGE_BASE" "$model" high "$version" "$PERMISSION_MODE" "${PR_REVIEW_MODEL_TIMEOUT:-}" "${PR_REVIEW_CLAUDE_BUDGET:-}"
        # Implementation and policy changes invalidate cached phases too.
        sha256sum "$ROOT/bin/pr-review" "$ROOT"/lib/*.sh "$ROOT"/prompts/*
        if [[ $stage == synthesis ]]; then
            printf '%s\n' "$(context_hash)"
            sha256sum "$RUN_DIR/reviews/codex.md" "$RUN_DIR/reviews/claude-independent.md" "$RUN_DIR/prompts/synthesis-prompt.txt"
            if [[ -f $RUN_DIR/reviews/reassessment-input.json ]]; then sha256sum "$RUN_DIR/reviews/reassessment-input.json"; fi
        fi
    } | sha256sum | cut -d ' ' -f1
}
reusable() {
    local stage=$1 output=$2 fingerprint=$3
    [[ -s $output ]] && jq -e --arg stage "$stage" --arg fp "$fingerprint" --arg hash "$(hash_file "$output")" '
      .phases[$stage].status == "completed" and .phases[$stage].fingerprint == $fp and .phases[$stage].output_hash == $hash' "$RUN_DIR/manifest.json" >/dev/null
}
snapshot_files() {
    git -C "$WORKTREE" status --porcelain=v1 --untracked-files=all --ignored > "$RUN_DIR/logs/$ACTIVE_STAGE.files-before.txt"
}
check_model_integrity() {
    is_auth_probe && return 0
    local after before new
    verify_snapshot
    before=$RUN_DIR/logs/$ACTIVE_STAGE.files-before.txt
    after=$RUN_DIR/logs/$ACTIVE_STAGE.files-after.txt
    git -C "$WORKTREE" status --porcelain=v1 --untracked-files=all --ignored > "$after"
    new=$(comm -13 <(sort "$before") <(sort "$after"))
    if [[ -n $new ]]; then
        warn "Model created untracked or ignored files (retained): $new"
        manifest '.generated_files += [$files | split("\n")[]] | .generated_files |= unique' --arg files "$new"
    fi
}
stream_error() {
    # A JSONL writer may be midway through its final line. Ignore incomplete
    # records, and never print provider-supplied text into the terminal.
    jq -Rsr '[split("\n")[] | fromjson? | select(type=="object")] |
        if any(.[]; .type=="error" and ((.message // "" | tostring) | test("not a git repository"; "i"))) then "git_repository_unavailable"
        elif any(.[]; .type=="turn.failed") then "codex_turn_failed"
        elif any(.[]; .type=="result" and .is_error==true) then "model_result_error"
        elif any(.[]; .type=="error") then "nonterminal_error"
        else "" end' "$1"
}
heartbeat() {
    local pid=$1 events=$2 stderr=$3 failure=$4 start=$SECONDS last=$SECONDS size=0 current reason
    local error_warned=false silence_warned=false
    while kill -0 "$pid" 2>/dev/null; do
        sleep 5
        kill -0 "$pid" 2>/dev/null || return 0
        current=$(stat -c %s -- "$events" "$stderr" 2>/dev/null || true)
        if [[ $current != "$size" ]]; then
            last=$SECONDS; size=$current; silence_warned=false
            reason=$(stream_error "$events")
            if [[ -n $reason && $reason != nonterminal_error ]]; then
                printf '%s\n' "$reason" > "$failure"
                warn "$ACTIVE_STAGE emitted a terminal error; stopping the model. See $events"
                kill -TERM -- "-$pid" 2>/dev/null || true
                sleep 5
                kill -KILL -- "-$pid" 2>/dev/null || true
                return 0
            elif [[ $reason == nonterminal_error ]] && ! $error_warned; then
                warn "$ACTIVE_STAGE reported an error event and may be retrying. See $events"
                error_warned=true
            fi
        fi
        printf '[%s] Process running: %ss elapsed; %ss since last output (stdout/stderr)\n' "$ACTIVE_STAGE" "$((SECONDS-start))" "$((SECONDS-last))" >&2
        if ((SECONDS-last >= 60)) && ! $silence_warned; then
            warn "No output for 60s; progress is unconfirmed. Logs: $events and $stderr. Ctrl-C stops this stage and retains completed reviews."
            silence_warned=true
        fi
    done
}
report_model_failure() {
    local cli=$1 rc=$2 events=$3 stderr=$4 model subtype
    if [[ $cli == codex ]]; then model=$CODEX_MODEL; else model=$CLAUDE_MODEL; fi
    model=${5:-$model}
    # JSON-mode CLIs can put their error result on stdout and leave stderr empty.
    # Report locations and a constrained status code, never arbitrary provider text.
    subtype=$(jq -sr '[.[] | select(type=="object" and .type=="result")][-1].subtype // empty |
        select(type=="string" and test("^[A-Za-z0-9_.-]{1,80}$"))' "$events" 2>/dev/null || true)
    warn "$ACTIVE_STAGE failed with exit $rc ($cli, model: $model)."
    case $(stream_error "$events") in
        git_repository_unavailable) warn "Codex rejected the working directory as a Git repository: $WORKTREE" ;;
        codex_turn_failed) warn 'Codex reported a failed turn in its event stream.' ;;
    esac
    if is_auth_probe; then report_probe_failure "$cli" "$rc" "$events" "$stderr"; fi
    if [[ -n $subtype ]]; then warn "CLI result subtype: $subtype"; fi
    if jq -se '[.[] | select(type=="object" and .type=="result")][-1].is_error == true' "$events" >/dev/null 2>&1; then
        warn 'CLI result reports an error (is_error=true), regardless of its subtype.'
        if [[ $cli == claude ]] && jq -se '[.[] | select(type=="object" and .type=="result")][-1].result |
            select(type=="string") | test("OAuth session expired"; "i")' "$events" >/dev/null 2>&1; then
            manifest '.phases[$stage].failure_reason = "oauth_session_expired"' --arg stage "$ACTIVE_STAGE"
            warn "Claude's OAuth session expired. Run 'claude auth login', then resume this run."
        fi
    fi
    warn "Captured stdout (including structured errors): $events"
    if [[ -s $stderr ]]; then warn "Captured stderr: $stderr"
    else warn "Captured stderr is empty: $stderr"; fi
}
run_model() {
    local cli=$1 input=$2 events=$3 stderr=$4; shift 4
    local rc=0 cwd=$WORKTREE model reason failure=$RUN_DIR/logs/$ACTIVE_STAGE.terminal-error
    if [[ $cli == codex ]]; then model=$CODEX_MODEL; else model=$CLAUDE_MODEL; fi
    if is_auth_probe; then
        cwd=$SCRATCH/auth-probe
        if [[ $cli == codex ]]; then model=${PR_REVIEW_CODEX_PROBE_MODEL:-gpt-6-luna}; else model=haiku; fi
    fi
    local -a command=("$@") prefix=()
    model_environment "$cli"
    if [[ -n ${PR_REVIEW_MODEL_TIMEOUT:-} ]]; then prefix=(timeout --signal=TERM "${PR_REVIEW_MODEL_TIMEOUT}s"); fi
    if is_auth_probe; then prefix=(timeout --signal=TERM --kill-after=5s "${PR_REVIEW_MODEL_TIMEOUT}s"); fi
    manifest '.phases[$stage] = {status:"running",fingerprint:$fp,started_at:$time,stdout_log:$stdout,stderr_log:$stderr}' --arg stage "$ACTIVE_STAGE" --arg fp "$FINGERPRINT" --arg time "$(now)" --arg stdout "$events" --arg stderr "$stderr"
    manifest '.phases[$stage].model = $model' --arg stage "$ACTIVE_STAGE" --arg model "$model"
    jq --arg stage "$ACTIVE_STAGE" --arg time "$(now)" --arg cwd "$cwd" --argjson argv "$(jq -cn --args '$ARGS.positional' -- "${command[@]}")" \
        '. + [{stage:$stage,time:$time,cwd:$cwd,argv:$argv}]' "$RUN_DIR/logs/commands.json" > "$RUN_DIR/logs/commands.json.tmp"
    mv -- "$RUN_DIR/logs/commands.json.tmp" "$RUN_DIR/logs/commands.json"
    if ! is_auth_probe; then verify_snapshot; snapshot_files; check_model_repository "$cli"; fi
    rm -f -- "$failure"
    # Job control gives each child its own process group, including CLI subagents.
    # Signal the entire group on interrupt, and wait before releasing the PR lock.
    set -m
    (exec {LOCK_FD}>&-; cd -- "$cwd" || exit; exec "${MODEL_ENV[@]}" "${prefix[@]}" "${command[@]}") < "$input" > "$events" 2> "$stderr" &
    MODEL_PID=$!
    (exec {LOCK_FD}>&-; heartbeat "$MODEL_PID" "$events" "$stderr" "$failure") &
    HEARTBEAT_PID=$!
    wait "$MODEL_PID" || rc=$?
    # Also stop descendants if the CLI exited on TERM before its children did.
    if [[ -s $failure ]]; then kill -KILL -- "-$MODEL_PID" 2>/dev/null || true; fi
    MODEL_PID=
    kill -- "-$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
    HEARTBEAT_PID=
    set +m
    reason=$(stream_error "$events")
    if [[ -s $failure ]]; then reason=$(cat "$failure"); fi
    if [[ -n $reason && $reason != nonterminal_error ]]; then
        manifest '.phases[$stage].failure_reason = $reason | .phases[$stage].process_exit_status = $rc' --arg stage "$ACTIVE_STAGE" --arg reason "$reason" --argjson rc "$rc"
        if ((rc==0)); then rc=1; fi
    fi
    manifest '.phases[$stage].exit_status = $rc | .phases[$stage].finished_at = $time' --arg stage "$ACTIVE_STAGE" --argjson rc "$rc" --arg time "$(now)"
    check_model_integrity
    if ((rc != 0)); then
        manifest '.phases[$stage].status = "failed"' --arg stage "$ACTIVE_STAGE"
        report_model_failure "$cli" "$rc" "$events" "$stderr" "$model"
        ACTIVE_STAGE=
        exit 1
    fi
}
check_model_repository() {
    local cli=$1 log=$RUN_DIR/logs/$ACTIVE_STAGE.git-preflight.log
    local -a identity=()
    if ! "${MODEL_ENV[@]}" git -c core.hooksPath=/dev/null -C "$WORKTREE" rev-parse \
        --path-format=absolute --show-toplevel --git-common-dir HEAD > "$log" 2> "$log.stderr"; then
        die "Git cannot read the worktree with $cli's filtered environment. See $log.stderr"
    fi
    mapfile -t identity < "$log"
    [[ ${identity[0]:-} == "$WORKTREE" && ${identity[1]:-} == "$COMMON_DIR" && ${identity[2]:-} == "$HEAD_SHA" ]] ||
        die "Git sees a different repository or snapshot in $cli's filtered environment. See $log"
}
complete_phase() {
    local output=$1
    [[ -s $output ]] || die "$ACTIVE_STAGE produced an empty report"
    manifest '.phases[$stage].status = "completed" | .phases[$stage].output_hash = $hash | .phases[$stage].exit_status = 0' --arg stage "$ACTIVE_STAGE" --arg hash "$(hash_file "$output")"
    ACTIVE_STAGE=
}
claude_common() {
    CLAUDE_COMMAND=(claude -p --model "$CLAUDE_MODEL" --effort high --permission-mode "$PERMISSION_MODE" --no-session-persistence)
    if [[ -n ${PR_REVIEW_CLAUDE_BUDGET:-} ]]; then CLAUDE_COMMAND+=(--max-budget-usd "$PR_REVIEW_CLAUDE_BUDGET"); fi
}
claude_result() {
    local raw=$1 result=$2
    jq -se 'map(select(.type == "result")) | last | select(.is_error != true and .subtype == "success")' "$raw" > "$result" || die "$ACTIVE_STAGE returned no successful Claude result event"
    manifest '.phases[$stage].permission_denials = $result[0].permission_denials // []' --arg stage "$ACTIVE_STAGE" --slurpfile result "$result"
}
run_claude_review() {
    local output=$RUN_DIR/reviews/claude-independent.md
    FINGERPRINT=$(phase_fingerprint claude)
    say '[4/7] Running independent Claude review'
    if reusable claude "$output" "$FINGERPRINT"; then say 'Reusing completed Claude review'; return; fi
    [[ ! -e $RUN_DIR/reviews/codex.md ]] || die 'Independent Claude inputs/output changed after Codex ran; start a new run to preserve independence'
    ACTIVE_STAGE=claude
    claude_common
    CLAUDE_COMMAND+=(--output-format stream-json --verbose --append-system-prompt "$(cat "$ROOT/prompts/independent.txt")" --disallowed-tools "${CLAUDE_DENY[@]}" -- "/code-review high --max-findings default $BASE_REF")
    run_model claude /dev/null "$RUN_DIR/reviews/claude-independent.jsonl" "$RUN_DIR/logs/claude-independent.stderr.log" "${CLAUDE_COMMAND[@]}"
    claude_result "$RUN_DIR/reviews/claude-independent.jsonl" "$RUN_DIR/logs/claude-result.json"
    jq -er '.result | select(type == "string" and test("\\S"))' "$RUN_DIR/logs/claude-result.json" > "$output.tmp" || die 'Claude returned an empty independent review'
    mv -- "$output.tmp" "$output"
    complete_phase "$output"
}
run_codex_review() {
    local output=$RUN_DIR/reviews/codex.md
    FINGERPRINT=$(phase_fingerprint codex)
    say '[5/7] Running Codex review'
    if reusable codex "$output" "$FINGERPRINT"; then say 'Reusing completed Codex review'; return; fi
    ACTIVE_STAGE=codex
    rm -f -- "$output.tmp"
    run_model codex /dev/null "$RUN_DIR/logs/codex.events.jsonl" "$RUN_DIR/logs/codex.stderr.log" \
        codex exec review -m "$CODEX_MODEL" --strict-config -c model_reasoning_effort=high -c sandbox_mode=read-only -c approval_policy=never --ephemeral --json -o "$output.tmp" --base "$BASE_REF"
    [[ -s $output.tmp ]] || die 'Codex returned an empty review'
    mv -- "$output.tmp" "$output"
    complete_phase "$output"
}
render_synthesis_prompt() {
    cat -- "$ROOT/prompts/synthesis.txt" > "$RUN_DIR/prompts/synthesis-prompt.txt.tmp"
    jq -n --arg repo "$HOST/$REPO" --arg pr "$PR" --arg head "$HEAD_SHA" --arg merge "$MERGE_BASE" --arg context "$RUN_DIR/context" --arg codex "$RUN_DIR/reviews/codex.md" --arg claude "$RUN_DIR/reviews/claude-independent.md" \
        --slurpfile manifest "$RUN_DIR/manifest.json" '{repository:$repo,pr:$pr,head:$head,merge_base:$merge,context_directory:$context,codex_review:$codex,claude_review:$claude,context_files:["pr.json","pr-discussion.json","pr-reviews.json","pr-inline-comments.json","ci-actions-runs.json","ci-statuses.json","pr.diff","diffstat.txt"],warnings:$manifest[0].warnings,generated_files:$manifest[0].generated_files}' >> "$RUN_DIR/prompts/synthesis-prompt.txt.tmp"
    mv -- "$RUN_DIR/prompts/synthesis-prompt.txt.tmp" "$RUN_DIR/prompts/synthesis-prompt.txt"
    if [[ -f $RUN_DIR/reviews/reassessment-input.json ]]; then
        printf '\nFresh-discussion reassessment:\n' >> "$RUN_DIR/prompts/synthesis-prompt.txt"
        cat -- "$RUN_DIR/reviews/reassessment-input.json" >> "$RUN_DIR/prompts/synthesis-prompt.txt"
    fi
}
run_synthesis() {
    local output=$RUN_DIR/reviews/synthesis.json
    render_synthesis_prompt
    FINGERPRINT=$(phase_fingerprint synthesis)
    say '[6/7] Running adversarial synthesis'
    if reusable synthesis "$output" "$FINGERPRINT" && jq -e -f "$ROOT/prompts/validate-synthesis.jq" "$output" >/dev/null; then
        say 'Reusing completed synthesis'
    else
        ACTIVE_STAGE=synthesis
        claude_common
        CLAUDE_COMMAND+=(--output-format json --json-schema "$(cat "$ROOT/prompts/synthesis.schema.json")" --disallowed-tools "${CLAUDE_DENY[@]}" --add-dir "$RUN_DIR/context" --add-dir "$RUN_DIR/reviews")
        if [[ -f $RUN_DIR/reviews/reassessment-input.json ]]; then CLAUDE_COMMAND+=(--add-dir "$RUN_DIR/discussion"); fi
        run_model claude "$RUN_DIR/prompts/synthesis-prompt.txt" "$RUN_DIR/logs/synthesis.raw.json" "$RUN_DIR/logs/synthesis.stderr.log" "${CLAUDE_COMMAND[@]}"
        claude_result "$RUN_DIR/logs/synthesis.raw.json" "$RUN_DIR/logs/synthesis-result.json"
        jq -e '.structured_output // (.result | fromjson)' "$RUN_DIR/logs/synthesis-result.json" > "$output.tmp" || die 'Synthesis did not return structured output'
        jq -e -f "$ROOT/prompts/validate-synthesis.jq" "$output.tmp" >/dev/null || die 'Synthesis violates the JSON schema; resume to retry'
        mv -- "$output.tmp" "$output"
        # Publication drafts are assembled from selected, discussion-aware items.
        complete_phase "$output"
    fi
    jq -r .report_md "$output" > "$RUN_DIR/reviews/synthesis.md.tmp"
    {
        printf '\n\n## Orchestrator coverage notes\n\nReviewed head `%s`, merge-base `%s`.\n' "$HEAD_SHA" "$MERGE_BASE"
        printf '\nCI covers Actions runs and legacy statuses only. Third-party GitHub App check runs are not visible. Empty results do not mean CI passed.\n'
        jq -r '.warnings[] | "\n- " + .' "$RUN_DIR/manifest.json"
        jq -r '.generated_files[] | "\n- Retained generated file: " + .' "$RUN_DIR/manifest.json"
    } >> "$RUN_DIR/reviews/synthesis.md.tmp"
    mv -- "$RUN_DIR/reviews/synthesis.md.tmp" "$RUN_DIR/reviews/synthesis.md"
}
run_reviews() {
    prepare_review_restart
    run_claude_review
    run_codex_review
    run_synthesis
}

# A changed or damaged independent review invalidates its downstream phases.
# Keep the archive outside the active run so independent reviewers receive no
# earlier cross-model reports. Persist the move list before changing any files.
prepare_review_restart() {
    local from='' archive file
    local -a files=()
    if ! jq -e '.review_restart != null' "$RUN_DIR/manifest.json" >/dev/null; then
        if ! reusable claude "$RUN_DIR/reviews/claude-independent.md" "$(phase_fingerprint claude)"; then
            if [[ -e $RUN_DIR/reviews/claude-independent.md || -e $RUN_DIR/reviews/codex.md ]] ||
                jq -e '.phases.claude != null or .phases.codex != null' "$RUN_DIR/manifest.json" >/dev/null; then from=claude; fi
        elif ! reusable codex "$RUN_DIR/reviews/codex.md" "$(phase_fingerprint codex)"; then
            if [[ -e $RUN_DIR/reviews/codex.md ]] || jq -e '.phases.codex != null' "$RUN_DIR/manifest.json" >/dev/null; then from=codex; fi
        fi
        [[ -n $from ]] || return 0
        # Finding IDs belong to the original analysis. Replacing that analysis
        # after a publication attempt could attach old actions to new findings.
        jq -e 'all(.[]; .status=="draft")' "$RUN_DIR/publication/actions.json" >/dev/null ||
            die 'Review inputs changed after publication was attempted; preserve this run and start a new review to keep publication history associated with its original findings'
        mkdir -p -- "$RUN_DIR-history"
        archive=$(mktemp -d "$RUN_DIR-history/restart-XXXXXX")
        mkdir -p -- "$archive"/{reviews,prompts,logs}
        cp -- "$RUN_DIR/manifest.json" "$archive/manifest.json"
        cp -a -- "$RUN_DIR/publication" "$archive/publication"
        for file in "$RUN_DIR"/reviews/* "$RUN_DIR"/prompts/* \
            "$RUN_DIR"/logs/claude-* "$RUN_DIR"/logs/claude.* "$RUN_DIR"/logs/codex.* \
            "$RUN_DIR"/logs/synthesis* "$RUN_DIR"/logs/contributions*; do
            [[ -f $file ]] || continue
            if [[ $from == codex && ( $file == "$RUN_DIR"/reviews/claude-* || $file == "$RUN_DIR"/logs/claude-* || $file == "$RUN_DIR"/logs/claude.* ) ]]; then continue; fi
            files+=("$file")
        done
        manifest '.review_restart = {from:$from,archive:$archive,files:$files}' --arg from "$from" --arg archive "$archive" \
            --argjson files "$(jq -cn --args '$ARGS.positional' -- "${files[@]}")"
    fi
    from=$(jq -er .review_restart.from "$RUN_DIR/manifest.json")
    archive=$(jq -er .review_restart.archive "$RUN_DIR/manifest.json")
    say "Restarting reviews from $from because saved inputs or outputs are no longer reusable."
    say "Previous reports and drafts are preserved in: $archive"
    while IFS= read -r file; do
        [[ -f $file ]] || continue
        mv -- "$file" "$archive/${file#"$RUN_DIR/"}"
    done < <(jq -r '.review_restart.files[]' "$RUN_DIR/manifest.json")
    manifest 'if $from=="claude" then del(.phases.claude) else . end |
        del(.phases.codex,.phases.synthesis,.phases.contributions,.reassessment) |
        .review_history += [{from:$from,archive:$archive,restarted_at:$time}] | del(.review_restart)' \
        --arg from "$from" --arg archive "$archive" --arg time "$(now)"
}
