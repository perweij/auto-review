# Fresh, complete conversation snapshots. Missing context disables publication.
DISCUSSION_READY=false DISCUSSION_HASH='' DISCUSSION_FILE=''
collect_discussion() {
    local dir cursor='' previous='' next
    local -a cursor_args
    DISCUSSION_READY=false
    dir=$(mktemp -d "$RUN_DIR/discussion/snapshot-XXXXXX")
    printf '[]\n' > "$dir/threads.json"
    for next in issues reviews inline; do
        local endpoint
        case $next in
            issues) endpoint="repos/$REPO/issues/$PR/comments" ;;
            reviews) endpoint="repos/$REPO/pulls/$PR/reviews" ;;
            inline) endpoint="repos/$REPO/pulls/$PR/comments" ;;
        esac
        if ! api_pages "$endpoint" "$dir/$next.json" 2> "$dir/$next.stderr.log"; then
            discussion_unavailable "Cannot retrieve complete $next discussion"; return
        fi
    done
    while true; do
        cursor_args=()
        [[ -z $cursor ]] || cursor_args=(-f "cursor=$cursor")
        if ! gh api --hostname "$HOST" graphql -f "query=$(cat "$ROOT/prompts/threads.graphql")" \
            -f "owner=${REPO%/*}" -f "name=${REPO##*/}" -F "number=$PR" "${cursor_args[@]}" > "$dir/page.json" 2> "$dir/threads.stderr.log"; then
            discussion_unavailable 'Cannot retrieve thread resolution status'; return
        fi
        if ! jq -e --argjson pr "$PR" '(.errors // [] | length)==0 and .data.repository.pullRequest.number == $pr and
            (.data.repository.pullRequest.reviewThreads | (.nodes|type)=="array" and (.pageInfo.hasNextPage|type)=="boolean")' "$dir/page.json" >/dev/null; then
            discussion_unavailable 'Incomplete GraphQL thread response'; return
        fi
        jq --slurpfile page "$dir/page.json" '. + $page[0].data.repository.pullRequest.reviewThreads.nodes' "$dir/threads.json" > "$dir/threads.json.tmp"
        mv -- "$dir/threads.json.tmp" "$dir/threads.json"
        [[ $(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' "$dir/page.json") == true ]] || break
        next=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // empty' "$dir/page.json")
        if [[ -z $next || $next == "$cursor" || $next == "$previous" ]]; then discussion_unavailable 'Invalid thread pagination cursor'; return; fi
        previous=$cursor; cursor=$next
    done
    if ! jq -n --slurpfile issues "$dir/issues.json" --slurpfile reviews "$dir/reviews.json" --slurpfile inline "$dir/inline.json" --slurpfile threads "$dir/threads.json" \
        -f "$ROOT/prompts/normalize-discussion.jq" > "$dir/discussion.json.tmp" 2> "$dir/normalize.stderr.log"; then
        discussion_unavailable 'Incomplete or inconsistent discussion snapshot'; return
    fi
    mv -- "$dir/discussion.json.tmp" "$dir/discussion.json"
    # Only exclude exact, confirmed own post IDs from freshness comparisons.
    # Their full text stays available to the matching model.
    jq -S --slurpfile actions "$RUN_DIR/publication/actions.json" '
      [$actions[0][] | select(.status=="published") |
        (if .kind=="reply" then .result.id else empty end), (.comment_results[]?.id)] as $replies |
      [$actions[0][] | select(.status=="published") |
        (if .kind=="review" then .result.id else .result.pull_request_review_id // empty end)] as $reviews |
      .items |= map(if .kind=="thread" then .comments |= map(select(.id as $id | $replies|index($id)|not)) |
          select((.comments|length)>0)
        elif .kind=="review" and (.comments[0].id as $id | $reviews|index($id)) then empty else . end)' \
        "$dir/discussion.json" > "$dir/external.json"
    DISCUSSION_HASH=$(hash_file "$dir/external.json")
    DISCUSSION_FILE=$dir/discussion.json
    DISCUSSION_READY=true
    manifest '.discussion = {complete:true,hash:$hash,path:$path,fetched_at:$time}' --arg hash "$DISCUSSION_HASH" --arg path "$DISCUSSION_FILE" --arg time "$(now)"
}
discussion_unavailable() {
    warn "$1. Publication paused; local reports and drafts are retained."
    manifest '.discussion.complete = false | .discussion.error = $error | .publication.status = "paused"' --arg error "$1"
}

# Optional review-status metadata is separate from semantic discussion hashes.
collect_review_status() {
    local cursor='' previous='' next dir
    local -a cursor_args
    dir=$(mktemp -d "$RUN_DIR/discussion/status-XXXXXX")
    printf '[]\n' > "$dir/reviews.json"
    printf '{"available":false}\n' > "$RUN_DIR/discussion/review-status.json.tmp"
    mv -- "$RUN_DIR/discussion/review-status.json.tmp" "$RUN_DIR/discussion/review-status.json"
    while true; do
        cursor_args=()
        [[ -z $cursor ]] || cursor_args=(-f "cursor=$cursor")
        if ! gh api --hostname "$HOST" graphql -f "query=$(cat "$ROOT/prompts/review-status.graphql")" \
            -f "owner=${REPO%/*}" -f "name=${REPO##*/}" -F "number=$PR" "${cursor_args[@]}" > "$dir/page.json" 2> "$dir/error.log"; then return 0; fi
        if ! jq -e --argjson pr "$PR" '(.errors // [] | length)==0 and (.data.viewer.login|type)=="string" and
            .data.repository.pullRequest.number==$pr and
            (.data.repository.pullRequest.latestOpinionatedReviews | (.nodes|type)=="array" and
            all(.nodes[]; (.author.login|type)=="string" and (.state|type)=="string") and
            (.pageInfo.hasNextPage|type)=="boolean")' "$dir/page.json" >/dev/null; then return 0; fi
        jq --slurpfile page "$dir/page.json" '. + $page[0].data.repository.pullRequest.latestOpinionatedReviews.nodes' "$dir/reviews.json" > "$dir/reviews.tmp"
        mv -- "$dir/reviews.tmp" "$dir/reviews.json"
        [[ $(jq -r '.data.repository.pullRequest.latestOpinionatedReviews.pageInfo.hasNextPage' "$dir/page.json") == true ]] || break
        next=$(jq -r '.data.repository.pullRequest.latestOpinionatedReviews.pageInfo.endCursor // empty' "$dir/page.json")
        if [[ -z $next || $next == "$cursor" || $next == "$previous" ]]; then return 0; fi
        previous=$cursor; cursor=$next
    done
    jq --slurpfile reviews "$dir/reviews.json" --arg time "$(now)" '.data.viewer.login as $viewer |
        .data.repository.pullRequest | {available:true,checked_at:$time,decision:.reviewDecision,
        latest_activity:.viewerLatestReview,opinionated:([$reviews[0][] | select((.author.login|ascii_downcase)==($viewer|ascii_downcase))] | first)}' \
        "$dir/page.json" > "$RUN_DIR/discussion/review-status.json.tmp"
    mv -- "$RUN_DIR/discussion/review-status.json.tmp" "$RUN_DIR/discussion/review-status.json"
}
