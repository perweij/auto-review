# Focused checks share snapshots, model execution and generation promotion.
prepare_analysis_kind() {
    local saved threads
    saved=$(jq -r '.analysis_kind // "full"' "$RUN_DIR/manifest.json")
    threads=$(jq -c '.followup_threads // []' "$RUN_DIR/manifest.json")
    if [[ $ANALYSIS_KIND == followup && -z $RESUME ]]; then die 'Follow-up requires an existing saved review'; fi
    if [[ $saved != "$ANALYSIS_KIND" || ( $ANALYSIS_KIND == followup && $threads != "$FOLLOWUP_THREADS" ) ]]; then
        if jq -e 'any(.[]; .status!="draft" and .status!="published")' "$RUN_DIR/publication/actions.json" >/dev/null; then
            die 'Resolve uncertain publication before changing the analysis scope; earlier receipts are retained'
        fi
        # Retain an incompatible unfinished attempt as history before replacing it.
        if [[ -L $SESSION_DIR/.pending-review ]]; then rm -f -- "$SESSION_DIR/.pending-review"; fi
        begin_review_attempt
        rm -rf -- "${RUN_DIR:?}/reviews" "$RUN_DIR/publication" "$RUN_DIR/prompts"
        mkdir -p -- "$RUN_DIR/reviews" "$RUN_DIR/publication" "$RUN_DIR/prompts"
        printf '[]\n' > "$RUN_DIR/publication/actions.json"
        manifest 'del(.phases.claude,.phases.codex,.phases.synthesis,.phases.contributions,.phases.followup,.reassessment,.publication_reset) |
            .publication={status:"not_published"} | .analysis_kind=$kind | .followup_threads=$threads' \
            --arg kind "$ANALYSIS_KIND" --argjson threads "$FOLLOWUP_THREADS"
    fi
}
render_followup_prompt() {
    cat -- "$ROOT/prompts/followup.txt"
    cat -- "$ROOT/prompts/synthesis-testing-focused.txt"
    jq -cn --arg input "$RUN_DIR/reviews/followup-input.json" '{input:$input}'
}
run_followup() {
    collect_discussion
    $DISCUSSION_READY || die 'Follow-up requires complete conversations'
    FOLLOWUP_INPUT=$SCRATCH/followup-input.json
    FOLLOWUP_PROMPT=$SCRATCH/followup-prompt.txt
    python3 "$ROOT/lib/followup-state.py" prepare "$RUN_DIR" "$FOLLOWUP_INPUT"
    render_followup_prompt > "$FOLLOWUP_PROMPT"
    FINGERPRINT=$(phase_fingerprint followup)
    if reusable followup "$RUN_DIR/reviews/followup.json" "$FINGERPRINT" &&
        python3 "$ROOT/lib/followup-state.py" validate "$RUN_DIR" "$RUN_DIR/reviews/followup.json"; then
        say 'Reusing completed follow-up assessment'
    else
        if jq -e '.phases.followup.status=="completed"' "$RUN_DIR/manifest.json" >/dev/null; then begin_review_attempt; fi
        cp -- "$FOLLOWUP_INPUT" "$RUN_DIR/reviews/followup-input.json"
        render_followup_prompt > "$RUN_DIR/prompts/followup-prompt.txt"
        unset FOLLOWUP_INPUT FOLLOWUP_PROMPT
        FINGERPRINT=$(phase_fingerprint followup)
        ACTIVE_STAGE=followup
        say "[followup] Checking selected conversations ($PROCESSING_PROVIDER)"
        review_command followup
        run_structured followup "$RUN_DIR/reviews/followup.json.tmp"
        python3 "$ROOT/lib/followup-state.py" validate "$RUN_DIR" "$RUN_DIR/reviews/followup.json.tmp"
        mv -- "$RUN_DIR/reviews/followup.json.tmp" "$RUN_DIR/reviews/followup.json"
        complete_phase "$RUN_DIR/reviews/followup.json"
    fi
    unset FOLLOWUP_INPUT FOLLOWUP_PROMPT
    # Record fresh state independently of the pinned analysis, so changes during
    # inference leave visibly stale assessments rather than silently reusing them.
    fetch_pr_metadata "$RUN_DIR/publication/pr-latest.json"
    collect_discussion
    $DISCUSSION_READY || die 'Cannot refresh conversations after follow-up; assessment retained for resume'
    collect_review_status
    python3 "$ROOT/lib/followup-state.py" adopt "$RUN_DIR"
    commit_review_attempt
}
