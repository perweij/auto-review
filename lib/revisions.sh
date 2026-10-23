# Preserve earlier snapshots and human edits outside active reviewer inputs.
# shellcheck disable=SC2030,SC2031
update_review_snapshot() {
    local old_head archive
    old_head=$(jq -r .snapshot.head "$RUN_DIR/manifest.json")
    [[ $(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir) == "$COMMON_DIR" &&
       $(git -C "$WORKTREE" rev-parse HEAD) == "$old_head" ]] || die 'Saved review worktree identity changed; inspect it before updating'
    if ! git -C "$WORKTREE" diff --quiet HEAD -- || ! git -C "$WORKTREE" diff --cached --quiet --; then
        die 'Saved review worktree has tracked changes; preserve or restore them before updating'
    fi
    # Resolve uncertain posts using the original head/markers before resetting
    # the action ledger. Actual GitHub posts enter the fresh discussion normally.
    (HEAD_SHA=$old_head; recover_actions)
    begin_review_attempt
    mkdir -p -- "$RUN_DIR-history"
    archive=$(mktemp -d "$RUN_DIR-history/snapshot-XXXXXX")
    cp -a -- "$RUN_DIR/." "$archive/"
    manifest '.snapshot_update = {archive:$archive,worktree:$worktree,
        run_id:$id,from:.snapshot,to:{head:$head,base:$base,merge_base:$merge,base_branch:$branch}}' \
        --arg archive "$archive" --arg worktree "$WORKTREE-${archive##*/}" \
        --arg id "${RUN_DIR##*/}-${archive##*/}" --arg head "$HEAD_SHA" --arg base "$BASE_SHA" --arg merge "$MERGE_BASE" --arg branch "$BASE_BRANCH"
    say "PR snapshot changed (head ${old_head:0:12} → ${HEAD_SHA:0:12}; merge-base $(jq -r '.snapshot.merge_base[:12]' "$RUN_DIR/manifest.json") → ${MERGE_BASE:0:12}). Updating this review."
    finish_snapshot_update
}

finish_snapshot_update() {
    local archive saved_worktree old_head new_head directory history_ref
    archive=$(jq -er .snapshot_update.archive "$RUN_DIR/manifest.json")
    saved_worktree=$(jq -er .snapshot_update.worktree "$RUN_DIR/manifest.json")
    old_head=$(jq -er .snapshot_update.from.head "$RUN_DIR/manifest.json")
    new_head=$(jq -er .snapshot_update.to.head "$RUN_DIR/manifest.json")
    [[ -s $archive/manifest.json ]] || die 'Snapshot archive missing; cannot update safely'
    history_ref=refs/pr-review/$PR/history/${archive##*/}
    git update-ref "$history_ref/head" "$old_head"
    git update-ref "$history_ref/base" "$(jq -r .snapshot_update.from.base "$RUN_DIR/manifest.json")"
    if [[ ! -e $saved_worktree ]]; then
        [[ $(git -C "$WORKTREE" rev-parse HEAD) == "$old_head" ]] || die 'Previous worktree changed during update'
        if ! git -C "$WORKTREE" diff --quiet HEAD -- || ! git -C "$WORKTREE" diff --cached --quiet --; then die 'Previous worktree has tracked changes during update'; fi
        git worktree move -- "$WORKTREE" "$saved_worktree" || die 'Cannot preserve old worktree; archive retained, retry to continue'
    fi
    [[ $(git -C "$saved_worktree" rev-parse HEAD) == "$old_head" &&
       $(git -C "$saved_worktree" rev-parse --path-format=absolute --git-common-dir) == "$COMMON_DIR" ]] || die 'Archived worktree identity mismatch'
    if [[ ! -e $WORKTREE ]]; then git worktree add --detach "$WORKTREE" "$new_head" || die 'Cannot prepare updated worktree; retry to continue'; fi
    [[ $(git -C "$WORKTREE" rev-parse HEAD) == "$new_head" &&
       $(git -C "$WORKTREE" rev-parse --path-format=absolute --git-common-dir) == "$COMMON_DIR" ]] || die 'Updated worktree identity mismatch'
    # The complete copy was saved before the journal. Repeating this cleanup is
    # safe: no model runs until the journal has been cleared atomically below.
    for directory in context prompts reviews publication logs discussion; do
        rm -rf -- "${RUN_DIR:?}/$directory"
        mkdir -p -- "$RUN_DIR/$directory"
    done
    printf '[]\n' > "$RUN_DIR/logs/commands.json"
    printf '[]\n' > "$RUN_DIR/publication/actions.json"
    manifest '.snapshot_history += [{archive:.snapshot_update.archive,worktree:.snapshot_update.worktree,snapshot:.snapshot_update.from}] |
        .previous_review={archive:.snapshot_update.archive,worktree:.snapshot_update.worktree} |
        .snapshot=.snapshot_update.to | .run_id=.snapshot_update.run_id |
        .phases={} | .publication={status:"not_published"} | .generated_files=[] | .warnings=[] |
        del(.snapshot_update,.reassessment,.review_restart,.agent_config_confirmed,.integrity_failed)'
    RUN_ID=$(jq -r .run_id "$RUN_DIR/manifest.json")
    say "Previous review and drafts preserved: $archive"
    say "Previous worktree preserved: $saved_worktree"
    say 'Reviewing the updated snapshot from scratch. Earlier drafts remain available for manual reuse.'
}

# Reading earlier edits is a local convenience after fresh review/matching.
# These drafts are never model inputs and are never inserted into a new draft.
prepare_previous_edits() {
    local report=$RUN_DIR/publication/previous-edits.md archive ledger head count generated expected
    : > "$report.tmp"
    while IFS= read -r archive; do
        ledger=$archive/publication/contributions.json
        if [[ ! -f $ledger ]]; then continue; fi
        if ! jq -e '(.proposals|type)=="array" and all(.proposals[];
            (.body_md|type)=="string" and (.basis.body_md|type)=="string" and (.references|type)=="array")' "$ledger" >/dev/null 2>&1; then
            warn "Cannot inspect earlier draft metadata: $ledger"; continue
        fi
        if ! head=$(jq -r '.snapshot.head // "unknown"' "$archive/manifest.json" 2>/dev/null); then
            warn "Cannot inspect earlier review metadata: $archive/manifest.json"; continue
        fi
        count=$(jq '[.proposals[] | select((.body_md|sub("\n+$";"")) != (.basis.body_md|sub("\n+$";"")))]|length' "$ledger")
        if ((count>0)); then
            printf '\n## Edited proposals from commit %s\n\nSaved review: %s\n' "$head" "$archive" >> "$report.tmp"
            jq -r '.proposals[] | select((.body_md|sub("\n+$";"")) != (.basis.body_md|sub("\n+$";""))) |
                "\n### Earlier \(.id) → \(.destination.url)\n\n\(.body_md)\n"' "$ledger" >> "$report.tmp"
        fi
        if [[ -f $archive/publication/review-body.md ]]; then
            generated=$(jq -r '.main_generated_hash // empty' "$ledger")
            if [[ -z $generated ]]; then
                # Older runs did not record the generated body hash. Recreate
                # it from that run's own selection, never from current findings.
                expected=$SCRATCH/earlier-generated-body.md
                write_main_body "$ledger" "$head" > "$expected"
                generated=$(hash_file "$expected")
            fi
            if [[ $(hash_file "$archive/publication/review-body.md") != "$generated" ]]; then
                { printf '\n## Edited main draft from commit %s\n\nSaved draft: %s/publication/review-body.md\n\n' "$head" "$archive"
                  cat -- "$archive/publication/review-body.md"
                  printf '\n'; } >> "$report.tmp"
            fi
        fi
    done < <(jq -r '[(.snapshot_history[]?.archive),(.review_history[]?.archive),(.previous_review.archive // empty)] | unique[]' "$RUN_DIR/manifest.json")
    if [[ -s $report.tmp ]]; then
        { printf '# Earlier edited drafts\n\nThese belong to earlier reviews. Check their relevance and locations before copying text into the current draft with [e ID] or [e]. No text has been copied automatically.\n'
          cat -- "$report.tmp"; } > "$report"
    else rm -f -- "$report"; fi
    rm -f -- "$report.tmp"
}
