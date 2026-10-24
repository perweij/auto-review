# Human-confirmed, resumable publication batches.
revalidate_publication() {
    local new_base new_merge advanced live_ref=refs/pr-review/$PR/publication-base
    verify_snapshot
    [[ $(jq -r '.integrity_failed // false' "$RUN_DIR/manifest.json") != true ]] || die 'Earlier worktree integrity failure blocks publication; start a fresh run'
    fetch_pr_metadata "$RUN_DIR/publication/pr-latest.json"
    [[ $(jq -r .headRefOid "$RUN_DIR/publication/pr-latest.json") == "$HEAD_SHA" ]] || die "PR head changed; publication blocked. Rerun pr-review $PR to update this saved review."
    [[ $(jq -r .state "$RUN_DIR/publication/pr-latest.json") == OPEN ]] || die 'PR is no longer open; publication blocked'
    if [[ $EVENT != COMMENT && $(jq -r '.author.login | ascii_downcase' "$RUN_DIR/publication/pr-latest.json") == "${VIEWER,,}" ]]; then die 'GitHub does not allow self-approval or self-request-changes'; fi
    git fetch --no-tags --refmap= --no-write-fetch-head "$REMOTE" "+refs/heads/$(jq -r .baseRefName "$RUN_DIR/publication/pr-latest.json"):$live_ref" || die 'Cannot revalidate base before publication'
    new_base=$(git rev-parse "$live_ref")
    new_merge=$(git merge-base "$HEAD_SHA" "$new_base") || die 'Cannot revalidate merge-base'
    [[ $new_merge == "$MERGE_BASE" ]] || die "Merge-base changed; publication blocked. Rerun pr-review $PR to update this saved review."
    if [[ $new_base != "$BASE_SHA" ]]; then
        advanced=$(git rev-list --count "$BASE_SHA..$new_base")
        say "Note: base has advanced $advanced commits since review; reviewed snapshot is unchanged."
    fi
}

actions_update() {
    local filter=$1; shift
    jq "$@" "$filter" "$RUN_DIR/publication/actions.json" > "$RUN_DIR/publication/actions.json.tmp"
    mv -- "$RUN_DIR/publication/actions.json.tmp" "$RUN_DIR/publication/actions.json"
}
publication_status() {
    local count
    count=$(jq '[.[]|select(.status=="published")]|length' "$RUN_DIR/publication/actions.json")
    manifest '.publication.published_actions=$count | .publication.status=$status' --argjson count "$count" --arg status "$1"
}
find_action_on_github() {
    local id=$1 kind marker original_viewer root endpoint
    kind=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.kind' "$RUN_DIR/publication/actions.json")
    marker=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.marker' "$RUN_DIR/publication/actions.json")
    original_viewer=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.viewer' "$RUN_DIR/publication/actions.json")
    root=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.target.root_id // 0' "$RUN_DIR/publication/actions.json")
    if [[ $kind == reply ]]; then endpoint="repos/$REPO/pulls/$PR/comments"; else endpoint="repos/$REPO/pulls/$PR/reviews"; fi
    api_pages "$endpoint" "$RUN_DIR/publication/lookup-$id.json" 2> "$RUN_DIR/logs/lookup-$id.stderr.log" || die 'Cannot check prior publication; refusing to risk a duplicate'
    jq -e 'all(.[]; (.id|type)=="number" and (.user.login|type)=="string" and (.body|type)=="string")' \
        "$RUN_DIR/publication/lookup-$id.json" >/dev/null || die 'Malformed publication lookup; refusing to risk a duplicate'
    jq --arg marker "$marker" --arg viewer "${original_viewer,,}" --arg kind "$kind" --arg head "$HEAD_SHA" --argjson root "$root" '
      [.[] | select((.user.login|ascii_downcase)==$viewer and ((.body // "")|contains($marker)) and
        (if $kind=="reply" then .in_reply_to_id==$root else .commit_id==$head end))] | first // null' \
        "$RUN_DIR/publication/lookup-$id.json" > "$RUN_DIR/publication/found-$id.json" || die 'Cannot parse publication lookup'
    jq -e '. != null' "$RUN_DIR/publication/found-$id.json" >/dev/null
}
record_action_published() {
    local id=$1 file=$2
    actions_update 'map(if .id==$id then .status="published" | .result=$result[0] else . end)' --arg id "$id" --slurpfile result "$file"
    cp -- "$file" "$RUN_DIR/publication/result-$id.json.tmp"
    mv -- "$RUN_DIR/publication/result-$id.json.tmp" "$RUN_DIR/publication/result-$id.json"
    publication_status partial
    say "Published $id: $(jq -r '.html_url // empty' "$file")"
}
recover_actions() {
    local id
    while IFS= read -r id; do
        if find_action_on_github "$id"; then record_action_published "$id" "$RUN_DIR/publication/found-$id.json"
        else actions_update 'map(if .id==$id then .status="failed" else . end)' --arg id "$id"; fi
    done < <(jq -r '.[]|select(.status=="submitting" or .status=="failed")|.id' "$RUN_DIR/publication/actions.json")
    if [[ $(jq 'length>0 and all(.[];.status=="published")' "$RUN_DIR/publication/actions.json") == true ]]; then publication_status published; fi
}
append_batch_action() {
    local spec=$1 id marker
    # A prior ambiguous action has already been recovered before this point.
    id=$(jq -r --argjson spec "$spec" --arg viewer "$VIEWER" '[.[] | select(.viewer==$viewer and .kind==$spec.kind and .target==$spec.target and .content==$spec.content and .event==$spec.event and .proposal_ids==$spec.proposal_ids)] | last | .id // empty' "$RUN_DIR/publication/actions.json")
    if [[ -z $id ]]; then
        id=a$(jq 'length+1' "$RUN_DIR/publication/actions.json")
        marker="<!-- pr-review run=$RUN_ID head=$HEAD_SHA action=$id -->"
        actions_update '. + [$spec + {id:$id,status:"draft",viewer:$viewer,marker:$marker,body_md:($spec.content+"\n\n"+$marker+"\n")}]' \
            --argjson spec "$spec" --arg id "$id" --arg viewer "$VIEWER" --arg marker "$marker"
    fi
    jq --arg id "$id" '. + [$id]' "$RUN_DIR/publication/batch.json" > "$RUN_DIR/publication/batch.json.tmp"
    mv -- "$RUN_DIR/publication/batch.json.tmp" "$RUN_DIR/publication/batch.json"
}
build_batch() {
    local replies_only=$1 spec main_count
    render_main_draft
    printf '[]\n' > "$RUN_DIR/publication/batch.json"
    while IFS= read -r spec; do append_batch_action "$spec"; done < <(
        jq -c --arg head "$HEAD_SHA" '[.proposals[] | select(.selected and (.published|not) and .available and .destination.kind=="reply")]
          | sort_by(.destination.key) | group_by(.destination.key) | .[] |
          {kind:"reply",target:.[0].destination,event:"COMMENT",proposal_ids:map(.id),finding_ids:[.[].basis.finding_ids[]],
           content:((map(.body_md)|join("\n\n"))+"\n\nReviewed commit: `"+$head+"`.")}' "$RUN_DIR/publication/contributions.json")
    main_count=$(jq '[.proposals[]|select(.selected and (.published|not) and .destination.kind=="review")]|length' "$RUN_DIR/publication/contributions.json")
    if ! $replies_only && { ((main_count > 0)) || [[ $EVENT != COMMENT ]]; }; then
        spec=$(jq -c --arg event "$EVENT" --rawfile body "$RUN_DIR/publication/review-body.md" \
            '[.proposals[]|select(.selected and (.published|not) and .destination.kind=="review")] |
             {kind:"review",target:null,event:$event,proposal_ids:map(.id),finding_ids:[.[].basis.finding_ids[]],content:$body}' "$RUN_DIR/publication/contributions.json")
        append_batch_action "$spec"
    fi
}
preview_batch() {
    jq -r --slurpfile batch "$RUN_DIR/publication/batch.json" '.[] | select(.id as $id | $batch[0]|index($id)) |
      "\n--- \(.id): \(.kind) \(.event) → \(.target.url // "main PR review") [\(.status)] ---\n\(.body_md)"' "$RUN_DIR/publication/actions.json"
}
check_batch_freshness() {
    collect_discussion
    BATCH_FRESH=false
    if ! $DISCUSSION_READY; then return; fi
    if [[ $DISCUSSION_HASH != "$(jq -r .discussion_hash "$RUN_DIR/publication/contributions.json")" ]]; then
        warn 'Discussion changed; remaining posts paused. Inspect the refreshed proposals.'
        reconcile_contributions
        return
    fi
    BATCH_FRESH=true
}
publish_batch() {
    local id kind root rc answer status
    local -a batch_ids
    BATCH_FRESH=true
    mapfile -t batch_ids < <(jq -r '.[]' "$RUN_DIR/publication/batch.json")
    for id in "${batch_ids[@]}"; do
        status=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.status' "$RUN_DIR/publication/actions.json")
        [[ $status != published ]] || continue
        if [[ $status == failed || $status == submitting ]]; then
            if find_action_on_github "$id"; then record_action_published "$id" "$RUN_DIR/publication/found-$id.json"; continue; fi
        fi
        revalidate_publication
        check_batch_freshness
        if ! $BATCH_FRESH; then return; fi
        kind=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.kind' "$RUN_DIR/publication/actions.json")
        root=$(jq -r --arg id "$id" '.[]|select(.id==$id)|.target.root_id // 0' "$RUN_DIR/publication/actions.json")
        if [[ $kind == reply ]]; then
            jq -e --argjson root "$root" 'any(.items[]; .kind=="thread" and .root_id==$root and .can_reply)' "$DISCUSSION_FILE" >/dev/null || die 'Thread is missing or no longer permits replies; remaining posts paused'
        fi
        # Draft body and destination are immutable after the displayed preview.
        jq -jr --arg id "$id" '.[]|select(.id==$id)|.body_md' "$RUN_DIR/publication/actions.json" > "$RUN_DIR/publication/submitted-$id.md"
        actions_update 'map(if .id==$id then .status="submitting" else . end)' --arg id "$id"
        publication_status submitting
        rc=0
        if [[ $kind == reply ]]; then
            gh api --hostname "$HOST" -X POST "repos/$REPO/pulls/$PR/comments/$root/replies" -F "body=@$RUN_DIR/publication/submitted-$id.md" \
                > "$RUN_DIR/publication/response-$id.json" 2> "$RUN_DIR/logs/publication.stderr.log" || rc=$?
        else
            gh api --hostname "$HOST" -X POST "repos/$REPO/pulls/$PR/reviews" -f "commit_id=$HEAD_SHA" -f "event=$EVENT" -F "body=@$RUN_DIR/publication/submitted-$id.md" \
                > "$RUN_DIR/publication/response-$id.json" 2> "$RUN_DIR/logs/publication.stderr.log" || rc=$?
        fi
        if ((rc == 0)) && jq -e --arg kind "$kind" --arg head "$HEAD_SHA" --argjson root "$root" \
            '(.id|type)=="number" and (if $kind=="reply" then .in_reply_to_id==$root else .commit_id==$head end)' "$RUN_DIR/publication/response-$id.json" >/dev/null; then
            record_action_published "$id" "$RUN_DIR/publication/response-$id.json"
        elif find_action_on_github "$id"; then
            record_action_published "$id" "$RUN_DIR/publication/found-$id.json"
        else
            actions_update 'map(if .id==$id then .status="failed" else . end)' --arg id "$id"
            if [[ $(jq '[.[]|select(.status=="published")]|length' "$RUN_DIR/publication/actions.json") == 0 ]]; then publication_status failed; else publication_status partial; fi
            warn "Action $id failed or had an ambiguous response; no matching post was found. Drafts retained."
            read -r -p 'Retry publication of remaining actions? Type yes: ' answer || answer=
            if [[ $answer == yes ]]; then
                warn 'Returning to the selection screen; preview and confirm remaining actions again.'
                BATCH_FRESH=false; return
            fi
            die "Publication stopped at $id; see $RUN_DIR/logs/publication.stderr.log"
        fi
    done
    publication_status published
}
show_proposals() {
    # ROOT comes from the entry point; lower-case root is a thread comment ID.
    # shellcheck disable=SC2153
    jq -r --slurpfile findings "$RUN_DIR/reviews/synthesis.json" --slurpfile discussion "$DISCUSSION_FILE" \
        -f "$ROOT/prompts/show-proposals.jq" "$RUN_DIR/publication/contributions.json"
}
show_selected_destinations() {
    jq -r '
      def counted($n;$one;$many): "\($n) " + (if $n==1 then $one else $many end);
      [.proposals[] | select(.selected and (.published|not) and .available)] as $selected |
      ([$selected[] | select(.destination.kind=="reply") | .destination.key] | unique | length) as $threads |
      ([$selected[] | select(.destination.kind=="review")] | length) as $main |
      if ($selected|length)==0 then "Selected contributions: none."
      else "Will post: " + ([
        (if $threads>0 then counted($threads;"thread reply";"thread replies") else empty end),
        (if $main>0 then "1 COMMENT review containing " + counted($main;"item";"items") else empty end)
      ] | join(" + ")) + "." end' "$RUN_DIR/publication/contributions.json"
}
edit_draft() {
    local file=$1 editor
    local -a command
    editor=${VISUAL:-${EDITOR:-vi}}
    read -r -a command <<< "$editor"
    (exec {LOCK_FD}>&-; "${command[@]}" "$file") || warn 'Editor failed'
}
inspect_proposal() {
    local id=$1
    jq -r --arg id "$id" --slurpfile discussion "$DISCUSSION_FILE" '.proposals[]|select(.id==$id)| . as $proposal |
      "\(.id) — \(.classification)\n\nDestination: \(if .destination.kind=="reply" then "Reply to existing thread" else "Main review item" end) → \(.destination.url)\n\nWhy: \(.basis.explanation)\n\nNew aspect: \(.basis.delta)\n\nDraft:\n\(.body_md)\n\nFull matched discussion:",
      ($discussion[0].items[] | select(.key as $key | $proposal.basis.discussion_keys|index($key)) |
        "\n--- \(.url // .comments[0].url)\(if .is_resolved then " (resolved)" else "" end)\(if .is_outdated then " (outdated)" else "" end) ---",
        (.comments[] | "\n@\(.author) · \(.created_at) · \(.url)\n\(.body)"))' \
      "$RUN_DIR/publication/contributions.json"
}
interactive_publication() {
    local choice id answer author replies_only
    # After adoption, both interfaces must use the same human draft ledger.
    # The adapter takes short transactional locks; release this CLI run's lock
    # before entering it. Models and matching have already completed.
    if [[ -f $RUN_DIR/publication/drafts.json ]]; then
        if $NO_PUBLISH; then say 'Saved API drafts are retained; open them in Magit or rerun without --no-publish.'; return; fi
        exec {LOCK_FD}>&-
        python3 "$ROOT/lib/api.py" --terminal "${SESSION_DIR:-$RUN_DIR}"
        return
    fi
    say "Full local review: $RUN_DIR/reviews/synthesis.md"
    if ! $CONTRIBUTIONS_READY; then say 'Publication paused: complete discussion and valid contribution proposals are required.'; return; fi
    say "Contribution report: $RUN_DIR/reviews/contributions.md"
    say "Main review draft: $RUN_DIR/publication/review-body.md"
    if [[ -s $RUN_DIR/publication/previous-edits.md ]]; then
        say "Earlier edited drafts are available for manual reuse: $RUN_DIR/publication/previous-edits.md"
    fi
    show_proposals
    say 'Destinations are assigned automatically: replies go to their threads; new items go in the main review.'
    if $NO_PUBLISH; then return; fi
    if [[ $(jq -r '.publication.status' "$RUN_DIR/manifest.json") == published && $(jq '[.proposals[]|select(.selected and (.published|not))]|length' "$RUN_DIR/publication/contributions.json") == 0 ]]; then say 'This run is already published.'; return; fi
    fetch_pr_metadata "$RUN_DIR/publication/pr-latest.json"
    if [[ $(jq -r .state "$RUN_DIR/publication/pr-latest.json") != OPEN ]]; then say 'PR is closed or merged; publication disabled.'; return; fi
    author=$(jq -r '.author.login|ascii_downcase' "$RUN_DIR/publication/pr-latest.json")
    while true; do
        say "Suggested action: $(jq -r .recommendation "$RUN_DIR/reviews/synthesis.json")"
        show_selected_destinations
        say '[n] Exit without publishing (default: Enter)  [c] Publish selected items (automatic destinations)'
        say '[t ID] Toggle  [i ID] Inspect proposal and discussion  [e ID] Edit proposal  [e] Edit main body  [s] Suppressed  [v] Preview selected drafts (no posting)  [f] Full report'
        if [[ -s $RUN_DIR/publication/previous-edits.md ]]; then say '[u] View earlier edited drafts for manual reuse'; fi
        if [[ $author != "${VIEWER,,}" ]]; then say '[r] Publish REQUEST_CHANGES  [a] Publish APPROVE'; fi
        read -r -p 'Choose action [n]: ' choice || choice=n
        read -r choice id <<< "$choice"
        case $choice in
            ''|n|N) say 'Leaving the review menu; saved reports and drafts are retained.'; return ;;
            t|i|e)
                if [[ -z $id && $choice == e ]]; then edit_draft "$RUN_DIR/publication/review-body.md"; continue; fi
                if ! jq -e --arg id "$id" 'any(.proposals[];.id==$id)' "$RUN_DIR/publication/contributions.json" >/dev/null; then warn 'Unknown proposal ID'; continue; fi
                if [[ $choice == i ]]; then inspect_proposal "$id"; continue; fi
                if ! jq -e --arg id "$id" 'any(.proposals[];.id==$id and (.published|not) and .available and .classification!="DUPLICATE")' "$RUN_DIR/publication/contributions.json" >/dev/null; then warn 'Posted, duplicate, or unavailable proposals cannot be selected or edited'; continue; fi
                if [[ $choice == t ]]; then ledger_update '(.proposals[]|select(.id==$id)|.selected) |= not' --arg id "$id"
                else
                    jq -r --arg id "$id" '.proposals[]|select(.id==$id)|.body_md' "$RUN_DIR/publication/contributions.json" > "$RUN_DIR/publication/edit-$id.md"
                    edit_draft "$RUN_DIR/publication/edit-$id.md"
                    if grep -q '[^[:space:]]' "$RUN_DIR/publication/edit-$id.md"; then
                        ledger_update '(.proposals[]|select(.id==$id)|.body_md)=$body' --arg id "$id" --rawfile body "$RUN_DIR/publication/edit-$id.md"
                    else warn 'Empty proposal edit was not applied'; fi
                fi
                render_main_draft; render_contribution_report; show_proposals; continue ;;
            s) jq -r '.proposals[]|select(.classification=="DUPLICATE")|"\(.id): \(.basis.explanation)\n"+([.references[].url]|join("\n"))' "$RUN_DIR/publication/contributions.json"; continue ;;
            f) cat -- "$RUN_DIR/reviews/synthesis.md"; continue ;;
            u) if [[ -s $RUN_DIR/publication/previous-edits.md ]]; then cat -- "$RUN_DIR/publication/previous-edits.md"; else say 'No earlier edited drafts were found.'; fi; continue ;;
            v)
                EVENT=COMMENT; build_batch false
                if [[ $(jq length "$RUN_DIR/publication/batch.json") == 0 ]]; then
                    say 'No selected unpublished drafts to preview. Use [t ID] to select a proposal or [i ID] to inspect one.'
                else preview_batch; fi
                continue ;;
            c|C) EVENT=COMMENT ;;
            p)
                say 'Replies-only shortcut: main review items will be skipped. Use [c] to publish all selected items at their assigned destinations.'
                EVENT=COMMENT ;;
            r|R|a|A)
                if [[ $author == "${VIEWER,,}" ]]; then warn 'Only COMMENT is available for your own PR'; continue; fi
                if [[ $choice == a || $choice == A ]]; then EVENT=APPROVE; else EVENT=REQUEST_CHANGES; fi ;;
            *) warn 'Unknown action'; continue ;;
        esac
        refresh_proposals
        if ! $CONTRIBUTIONS_READY; then return; fi
        if $PROPOSALS_CHANGED; then show_proposals; continue; fi
        revalidate_publication
        if [[ $EVENT == APPROVE ]] && (( $(jq .blocking_findings "$RUN_DIR/reviews/synthesis.json") > 0 )); then
            warn 'Blocking findings remain in this review, including any duplicates suppressed from publication.'
            read -r -p 'Approve despite blocking findings? Type yes: ' answer || answer=
            [[ $answer == yes ]] || continue
        fi
        replies_only=false; [[ $choice != p ]] || replies_only=true
        build_batch "$replies_only"
        if [[ $(jq length "$RUN_DIR/publication/batch.json") == 0 ]]; then
            if $replies_only; then
                say 'No selected unpublished thread replies. Use [v] to preview selected drafts or [i ID] to inspect a proposal and its discussion.'
            else say 'Nothing new is selected; no publication is needed.'; fi
            continue
        fi
        preview_batch
        read -r -p 'Publish this exact batch? Type yes: ' answer || answer=
        [[ $answer == yes ]] || return 0
        publish_batch
        if $BATCH_FRESH; then return; fi
        if ! $DISCUSSION_READY || ! $CONTRIBUTIONS_READY; then return; fi
        # Record posted items as immutable and omit them from the next preview.
        reconcile_contributions
        show_proposals
    done
}
