# Model lifecycle: explicit inputs, filtered environment, liveness, integrity.
model_environment() {
    local cli=$1 name upper
    local -a removed=()
    MODEL_ENV=(env)
    while IFS= read -r name; do
        upper=${name^^}
        case $upper in
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
heartbeat() {
    local pid=$1 events=$2 start=$SECONDS last=$SECONDS size=0 current
    while kill -0 "$pid" 2>/dev/null; do
        sleep 5
        current=$(stat -c %s -- "$events" 2>/dev/null || printf 0)
        if [[ $current != "$size" ]]; then last=$SECONDS; size=$current; fi
        printf '[%s] Working: %ss elapsed; %ss since last output\n' "$ACTIVE_STAGE" "$((SECONDS-start))" "$((SECONDS-last))" >&2
    done
}
run_model() {
    local cli=$1 input=$2 events=$3 stderr=$4; shift 4
    local rc=0
    local -a command=("$@") prefix=()
    model_environment "$cli"
    if [[ -n ${PR_REVIEW_MODEL_TIMEOUT:-} ]]; then prefix=(timeout --signal=TERM "${PR_REVIEW_MODEL_TIMEOUT}s"); fi
    manifest '.phases[$stage] = {status:"running",fingerprint:$fp,started_at:$time}' --arg stage "$ACTIVE_STAGE" --arg fp "$FINGERPRINT" --arg time "$(now)"
    jq --arg stage "$ACTIVE_STAGE" --arg time "$(now)" --arg cwd "$WORKTREE" --argjson argv "$(jq -cn --args '$ARGS.positional' -- "${command[@]}")" \
        '. + [{stage:$stage,time:$time,cwd:$cwd,argv:$argv}]' "$RUN_DIR/logs/commands.json" > "$RUN_DIR/logs/commands.json.tmp"
    mv -- "$RUN_DIR/logs/commands.json.tmp" "$RUN_DIR/logs/commands.json"
    verify_snapshot
    snapshot_files
    # Job control gives each child its own process group, including CLI subagents.
    # Signal the entire group on interrupt, and wait before releasing the PR lock.
    set -m
    (exec {LOCK_FD}>&-; cd -- "$WORKTREE" || exit; exec "${MODEL_ENV[@]}" "${prefix[@]}" "${command[@]}") < "$input" > "$events" 2> "$stderr" &
    MODEL_PID=$!
    (exec {LOCK_FD}>&-; heartbeat "$MODEL_PID" "$events") &
    HEARTBEAT_PID=$!
    wait "$MODEL_PID" || rc=$?
    MODEL_PID=
    kill -- "-$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
    HEARTBEAT_PID=
    set +m
    manifest '.phases[$stage].exit_status = $rc | .phases[$stage].finished_at = $time' --arg stage "$ACTIVE_STAGE" --argjson rc "$rc" --arg time "$(now)"
    check_model_integrity
    if ((rc != 0)); then
        manifest '.phases[$stage].status = "failed"' --arg stage "$ACTIVE_STAGE"
        # Do not replay arbitrary stderr, which could contain sensitive content.
        warn "$ACTIVE_STAGE failed with exit $rc (model: $cli). Check model availability, usage limits and $stderr"
        ACTIVE_STAGE=
        exit 1
    fi
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
    run_claude_review
    run_codex_review
    run_synthesis
}
