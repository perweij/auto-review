# Matching, proposal persistence and the editable main-review draft.
PROPOSALS_CHANGED=false CONTRIBUTIONS_READY=false
prepare_contributions() {
    recover_actions
    collect_discussion
    if $DISCUSSION_READY; then reconcile_contributions; fi
}
reconcile_contributions() {
    local output=$RUN_DIR/reviews/contributions.json previous_hash='' attempts=0
    CONTRIBUTIONS_READY=false
    if [[ -f $RUN_DIR/publication/contributions.json ]]; then previous_hash=$(jq -r .discussion_hash "$RUN_DIR/publication/contributions.json"); fi
    while true; do
        render_contributions_prompt
        FINGERPRINT=$(phase_fingerprint contributions)
        if reusable contributions "$output" "$FINGERPRINT" &&
            jq -e --slurpfile findings "$RUN_DIR/reviews/synthesis.json" --slurpfile discussion "$DISCUSSION_FILE" -f "$ROOT/prompts/validate-contributions.jq" "$output" >/dev/null; then break; fi
        protect_completed_review contributions
        output=$RUN_DIR/reviews/contributions.json
        render_contributions_prompt
        FINGERPRINT=$(phase_fingerprint contributions)
        say '[final] Matching findings against the complete discussion'
        ACTIVE_STAGE=contributions
        review_command contributions
        run_model claude "$RUN_DIR/prompts/contributions-prompt.txt" "$RUN_DIR/logs/contributions.raw.json" "$RUN_DIR/logs/contributions.stderr.log" "${REVIEW_COMMAND[@]}"
        claude_result "$RUN_DIR/logs/contributions.raw.json" "$RUN_DIR/logs/contributions-result.json"
        jq -e '.structured_output // (.result | fromjson)' "$RUN_DIR/logs/contributions-result.json" > "$output.tmp" || die 'No structured contribution result'
        jq -e --slurpfile findings "$RUN_DIR/reviews/synthesis.json" --slurpfile discussion "$DISCUSSION_FILE" -f "$ROOT/prompts/validate-contributions.jq" "$output.tmp" >/dev/null || die 'Invalid contribution schema, finding coverage, or discussion destination'
        mv -- "$output.tmp" "$output"
        if [[ $(jq '.validity_conflicts|length' "$output") == 0 ]]; then complete_phase "$output"; break; fi
        manifest '.phases.contributions.status = "needs_reassessment"'
        ACTIVE_STAGE=
        if ((attempts > 0)) || [[ $(jq -r '.reassessment.discussion_hash // empty' "$RUN_DIR/manifest.json") == "$DISCUSSION_HASH" ]]; then
            discussion_unavailable 'Fresh discussion still challenges finding validity after reassessment'; return
        fi
        attempts=$((attempts+1))
        jq -n --slurpfile prior "$RUN_DIR/reviews/synthesis.json" --slurpfile result "$output" --arg discussion "$DISCUSSION_FILE" \
            '{prior_findings:$prior[0].findings,validity_conflicts:$result[0].validity_conflicts,discussion:$discussion}' > "$RUN_DIR/reviews/reassessment-input.json"
        manifest '.reassessment = {discussion_hash:$hash,input:$input,discussion:$discussion}' --arg hash "$DISCUSSION_HASH" --arg input "$RUN_DIR/reviews/reassessment-input.json" --arg discussion "$DISCUSSION_FILE"
        say 'Fresh discussion challenges validity; refreshing synthesis, retaining independent reviews.'
        run_synthesis
    done
    local ledger=$RUN_DIR/publication/contributions.json old=$RUN_DIR/publication/contributions.previous.json
    if [[ -f $ledger ]]; then
        cp -- "$ledger" "$old"
        local archive
        archive=$(mktemp "$RUN_DIR/publication/contributions-history-XXXXXX.json")
        cp -- "$ledger" "$archive"
    else printf '{}\n' > "$old"; fi
    jq -n --slurpfile old "$old" --slurpfile model "$output" --slurpfile discussion "$DISCUSSION_FILE" --slurpfile actions "$RUN_DIR/publication/actions.json" \
        --arg hash "$DISCUSSION_HASH" --arg findings_hash "$(hash_file "$RUN_DIR/reviews/synthesis.json")" --arg pr_url "$PR_URL" \
        -f "$ROOT/prompts/merge-contributions.jq" > "$ledger.tmp"
    mv -- "$ledger.tmp" "$ledger"
    CONTRIBUTIONS_READY=true
    manifest 'if .publication.status=="paused" then .publication.status=(if (.publication.published_actions // 0)>0 then "partial" else "not_published" end) else . end'
    PROPOSALS_CHANGED=false
    if [[ -n $previous_hash && $previous_hash != "$DISCUSSION_HASH" ]]; then PROPOSALS_CHANGED=true; fi
    render_main_draft
    render_contribution_report
    prepare_previous_edits
    commit_review_attempt
}
ledger_update() {
    local filter=$1; shift
    jq "$@" "$filter" "$RUN_DIR/publication/contributions.json" > "$RUN_DIR/publication/contributions.json.tmp"
    mv -- "$RUN_DIR/publication/contributions.json.tmp" "$RUN_DIR/publication/contributions.json"
}
write_main_body() {
    local ledger=$1 head=$2
    jq -r --arg head "$head" '[.proposals[] | select(.selected and (.published|not) and .destination.kind=="review") |
       .body_md + (if (.references|length)>0 then "\n\nRelated discussion: " + ([.references[] | "[earlier discussion]("+.url+")"]|join(", ")) else "" end)] | join("\n\n---\n\n")
       | if test("\\S") then . else "Reviewed commit `"+$head+"`. No additional findings are being posted." end' "$ledger"
}
render_main_draft() {
    local signature
    signature=$(jq -c '[.proposals[] | select(.selected and (.published|not) and .destination.kind=="review") | {id,body_md,references}]' "$RUN_DIR/publication/contributions.json" | sha256sum | cut -d ' ' -f1)
    if [[ -f $RUN_DIR/publication/review-body.md && $(jq -r '.main_signature // empty' "$RUN_DIR/publication/contributions.json") == "$signature" ]]; then return; fi
    if [[ -f $RUN_DIR/publication/review-body.md ]]; then
        cp -- "$RUN_DIR/publication/review-body.md" "$(mktemp "$RUN_DIR/publication/review-body-history-XXXXXX.md")"
    fi
    write_main_body "$RUN_DIR/publication/contributions.json" "$HEAD_SHA" > "$RUN_DIR/publication/review-body.md.tmp"
    mv -- "$RUN_DIR/publication/review-body.md.tmp" "$RUN_DIR/publication/review-body.md"
    ledger_update '.main_signature=$signature | .main_generated_hash=$hash' --arg signature "$signature" --arg hash "$(hash_file "$RUN_DIR/publication/review-body.md")"
}
render_contribution_report() {
    jq -r '"# Proposed contributions\n", (.proposals[] |
      "\n## \(.id): \(.classification)\n\n\(.basis.explanation)\n\nNew contribution: \(.basis.delta)\n\nDestination: \(.destination.url)\n\n\(.body_md)\n" +
      ([.references[] | "- Related: "+.url]|join("\n")))' "$RUN_DIR/publication/contributions.json" > "$RUN_DIR/reviews/contributions.md"
}
refresh_proposals() {
    PROPOSALS_CHANGED=false
    collect_discussion
    if ! $DISCUSSION_READY; then CONTRIBUTIONS_READY=false; return; fi
    if [[ $(jq -r .discussion_hash "$RUN_DIR/publication/contributions.json") != "$DISCUSSION_HASH" ]]; then
        reconcile_contributions
        warn 'Discussion changed. Inspect the refreshed proposals before confirming again.'
    fi
}
render_contributions_prompt() {
    {
        cat -- "$ROOT/prompts/contributions.txt"
        jq -n --arg findings "$RUN_DIR/reviews/synthesis.json" --arg discussion "$DISCUSSION_FILE" --arg head "$HEAD_SHA" --slurpfile manifest "$RUN_DIR/manifest.json" \
            '{findings:$findings,discussion:$discussion,head:$head,reassessment:$manifest[0].reassessment}'
    } > "$RUN_DIR/prompts/contributions-prompt.txt"
}
