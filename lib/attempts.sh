# A session keeps its completed review while a replacement is built separately.
# Only the current-review symlink is switched at commit; no multi-file copy can
# expose a half-installed review. Candidate directories remain stable on resume.
SESSION_DIR=''
selected_run_directory() {
    local session=$1
    if [[ -L $session/.pending-review ]]; then realpath -e -- "$session/.pending-review"
    elif [[ -L $session/.current-review ]]; then realpath -e -- "$session/.current-review"
    else printf '%s\n' "$session"; fi
}
current_run_directory() {
    if [[ -L $SESSION_DIR/.current-review ]]; then realpath -e -- "$SESSION_DIR/.current-review"
    else printf '%s\n' "$SESSION_DIR"; fi
}
set_review_pointer() {
    local name=$1 target=$2
    # Replacing a symlink with rename is atomic, including across interruptions.
    rm -f -- "$SESSION_DIR/$name.tmp"
    ln -s -- "$target" "$SESSION_DIR/$name.tmp"
    mv -Tf -- "$SESSION_DIR/$name.tmp" "$SESSION_DIR/$name"
}
begin_review_attempt() {
    local candidate previous=$RUN_DIR
    if [[ -L $SESSION_DIR/.pending-review && $(realpath -e "$SESSION_DIR/.pending-review") == "$RUN_DIR" ]]; then return; fi
    mkdir -p -- "$SESSION_DIR-history"
    candidate=$(mktemp -d "$SESSION_DIR-history/attempt-XXXXXX")
    # The candidate is not discoverable until this complete copy is registered.
    # Copy only run artifacts: session selector links must never be inherited.
    cp -- "$RUN_DIR/manifest.json" "$candidate/manifest.json"
    local directory
    for directory in context prompts reviews publication logs discussion; do
        if [[ -d $RUN_DIR/$directory ]]; then cp -a -- "$RUN_DIR/$directory" "$candidate/"
        else mkdir -p -- "$candidate/$directory"; fi
    done
    RUN_DIR=$candidate
    manifest 'def relocated: if type=="string" and startswith($previous+"/") then $candidate + .[($previous|length):] else . end;
        .session_directory=$session | .attempt={status:"building",previous:$previous} |
        (if .discussion.path!=null then .discussion.path |= relocated else . end) |
        (if .reassessment!=null then .reassessment.input |= relocated | .reassessment.discussion |= relocated else . end)' \
        --arg session "$SESSION_DIR" --arg previous "$previous" --arg candidate "$candidate"
    if [[ -f $RUN_DIR/reviews/reassessment-input.json ]]; then
        jq --arg previous "$previous" --arg candidate "$candidate" '
            .discussion |= (if type=="string" and startswith($previous+"/") then $candidate + .[($previous|length):] else . end)' \
            "$RUN_DIR/reviews/reassessment-input.json" > "$RUN_DIR/reviews/reassessment-input.json.tmp"
        mv -- "$RUN_DIR/reviews/reassessment-input.json.tmp" "$RUN_DIR/reviews/reassessment-input.json"
    fi
    set_review_pointer .pending-review "$candidate"
    say "Building replacement review in: $candidate"
    if [[ -s $previous/reviews/synthesis.md ]]; then say "Previous review remains available: $previous/reviews/synthesis.md"
    else say "Previous artifacts remain available: $previous"; fi
}
commit_review_attempt() {
    local stage report
    $CONTRIBUTIONS_READY || return 0
    [[ -L $SESSION_DIR/.pending-review && $(realpath -e "$SESSION_DIR/.pending-review") == "$RUN_DIR" ]] || return 0
    jq -e 'all(.phases.claude,.phases.codex,.phases.synthesis,.phases.contributions; .status=="completed")' "$RUN_DIR/manifest.json" >/dev/null ||
        die 'Replacement review is incomplete; previous review remains current'
    for stage in claude codex synthesis contributions; do
        case $stage in
            claude) report=$RUN_DIR/reviews/claude-independent.md ;;
            codex) report=$RUN_DIR/reviews/codex.md ;;
            *) report=$RUN_DIR/reviews/$stage.json ;;
        esac
        [[ -s $report && $(hash_file "$report") == "$(jq -r --arg stage "$stage" '.phases[$stage].output_hash' "$RUN_DIR/manifest.json")" ]] ||
            die 'Replacement report changed after completion; previous review remains current'
    done
    manifest '.attempt.status="ready"'
    set_review_pointer .current-review "$RUN_DIR"
    rm -f -- "$SESSION_DIR/.pending-review"
    say 'Replacement review and drafts are ready; made them current.'
}

# Later-stage refreshes also stage their work, after the preceding phases have
# passed their normal reuse checks. Re-key only those verified phases whose
# command/prompt artifact paths changed when copied into the candidate.
protect_completed_review() {
    local stage=$1 previous=$RUN_DIR kept
    if [[ -L $SESSION_DIR/.pending-review ]]; then return 0; fi
    jq -e '.phases.contributions.status=="completed"' "$RUN_DIR/manifest.json" >/dev/null || return 0
    begin_review_attempt
    if [[ $DISCUSSION_FILE == "$previous/"* ]]; then DISCUSSION_FILE=$RUN_DIR/${DISCUSSION_FILE#"$previous/"}; fi
    for kept in claude codex; do
        manifest '.phases[$stage].inputs=$inputs | .phases[$stage].fingerprint=$fp' --arg stage "$kept" \
            --argjson inputs "$(phase_inputs "$kept")" --arg fp "$(phase_fingerprint "$kept")"
    done
    if [[ $stage == contributions ]]; then
        render_synthesis_prompt
        manifest '.phases.synthesis.inputs=$inputs | .phases.synthesis.fingerprint=$fp' \
            --argjson inputs "$(phase_inputs synthesis)" --arg fp "$(phase_fingerprint synthesis)"
    fi
}
