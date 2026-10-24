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
    # A retained review must remain readable after force-pushes and GC, even
    # when its worktree has subsequently been removed.
    local saved_head saved_merge history_id
    saved_head=$(jq -r '.snapshot.head // empty' "$RUN_DIR/manifest.json")
    saved_merge=$(jq -r '.snapshot.merge_base // empty' "$RUN_DIR/manifest.json")
    history_id=$(printf '%s' "$RUN_DIR" | sha256sum | cut -c1-24)
    if [[ -n $saved_head && -n $saved_merge ]]; then
        git update-ref "refs/pr-review/$PR/history/$history_id/head" "$saved_head"
        git update-ref "refs/pr-review/$PR/history/$history_id/merge-base" "$saved_merge"
    fi
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
        .session_directory=$session | .attempt={status:"building",previous:$previous} | del(.publication_reset) |
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

# Finding IDs are local to an analysis. Confirmed receipts from the preceding
# analysis stay in history and must not mark replacement findings as published.
# Discussion-only rematches keep their ledger; only a new analysis uses this.
prepare_replacement_publication() {
    local archive next_id
    if [[ -L $SESSION_DIR/.pending-review && $(realpath -e "$SESSION_DIR/.pending-review") == "$RUN_DIR" ]] &&
        jq -e '.publication_reset.status=="complete"' "$RUN_DIR/manifest.json" >/dev/null; then return 0; fi
    if ! jq -e '.publication_reset.status=="pending"' "$RUN_DIR/manifest.json" >/dev/null; then
        jq -e 'type=="array" and all(.[]; type=="object" and (.status|type)=="string")' "$RUN_DIR/publication/actions.json" >/dev/null ||
            die 'Cannot verify saved publication history; replacement stopped without discarding receipts'
        jq -e 'any(.[]; .status!="draft")' "$RUN_DIR/publication/actions.json" >/dev/null || return 0
        jq -e 'all(.[]; .status=="draft" or .status=="published")' "$RUN_DIR/publication/actions.json" >/dev/null ||
            die 'Resolve uncertain publication in the previous review before replacing its analysis; all drafts and receipts are retained'
        begin_review_attempt
        archive=$(jq -er '.attempt.previous' "$RUN_DIR/manifest.json")
        next_id="$RUN_ID-${RUN_DIR##*/}"
        manifest '.publication_reset={status:"pending",archive:$archive,run_id:$id}' --arg archive "$archive" --arg id "$next_id"
    fi
    finish_publication_reset
}

finish_publication_reset() {
    local archive
    archive=$(jq -er '.publication_reset.archive' "$RUN_DIR/manifest.json")
    [[ $archive != "$RUN_DIR" && -s $archive/publication/actions.json ]] ||
        die 'Previous publication history is missing; replacement stopped without discarding receipts'
    # The journal is durable before cleanup. No model or publication can run
    # until it finishes; interruption simply repeats this candidate-only reset.
    rm -rf -- "${RUN_DIR:?}/publication"
    mkdir -p -- "$RUN_DIR/publication"
    printf '[]\n' > "$RUN_DIR/publication/actions.json"
    manifest '.publication={status:"not_published",published_actions:0} |
        .run_id=.publication_reset.run_id | .publication_reset.status="complete" |
        .previous_review={archive:.publication_reset.archive,worktree:.worktree} |
        del(.phases.contributions)'
    RUN_ID=$(jq -r .run_id "$RUN_DIR/manifest.json")
    say "Earlier posts and edited drafts remain in review history: $archive"
    say 'The replacement will match findings against the discussion, including those earlier posts.'
}
commit_review_attempt() {
    local stage report previous promotion_fd
    $CONTRIBUTIONS_READY || return 0
    [[ -L $SESSION_DIR/.pending-review && $(realpath -e "$SESSION_DIR/.pending-review") == "$RUN_DIR" ]] || return 0
    local -a stages=(synthesis contributions)
    if dual_review; then stages=(claude codex "${stages[@]}"); fi
    jq -e --argjson stages "$(jq -cn --args '$ARGS.positional' -- "${stages[@]}")" '.phases as $phases | all($stages[]; $phases[.].status=="completed")' "$RUN_DIR/manifest.json" >/dev/null ||
        die 'Replacement review is incomplete; previous review remains current'
    for stage in "${stages[@]}"; do
        case $stage in
            claude) report=$RUN_DIR/reviews/claude-independent.md ;;
            codex) report=$RUN_DIR/reviews/codex.md ;;
            *) report=$RUN_DIR/reviews/$stage.json ;;
        esac
        [[ -s $report && $(hash_file "$report") == "$(jq -r --arg stage "$stage" '.phases[$stage].output_hash' "$RUN_DIR/manifest.json")" ]] ||
            die 'Replacement report changed after completion; previous review remains current'
    done
    # Serialize the pointer switch with short API draft transactions. A save
    # either finishes in the previous current generation or observes the new
    # pointer and reports a stale/historical write without losing editor text.
    previous=$(current_run_directory)
    exec {promotion_fd}>"$previous/publication/.drafts.lock"
    flock -x "$promotion_fd"
    manifest '.attempt.status="ready"'
    set_review_pointer .current-review "$RUN_DIR"
    rm -f -- "$SESSION_DIR/.pending-review"
    exec {promotion_fd}>&-
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
        dual_review || break
        manifest '.phases[$stage].inputs=$inputs | .phases[$stage].fingerprint=$fp' --arg stage "$kept" \
            --argjson inputs "$(phase_inputs "$kept")" --arg fp "$(phase_fingerprint "$kept")"
    done
    if [[ $stage == contributions ]]; then
        render_synthesis_prompt
        manifest '.phases.synthesis.inputs=$inputs | .phases.synthesis.fingerprint=$fp' \
            --argjson inputs "$(phase_inputs synthesis)" --arg fp "$(phase_fingerprint synthesis)"
    fi
}
