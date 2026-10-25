#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

api() {
    local request=$1 saved=$2
    jq -nc --arg session "$saved" --argjson request "$request" \
        '$request + {protocol:1,session:$session}' | "$CLI" api
}

api_mutate() {
    local operation=$1 fields=$2
    local state request
    state=$(api '{"command":"read"}' "$saved")
    request=$(jq -nc --arg op "$operation" --argjson state "$state" --argjson fields "$fields" \
        '$fields + {command:"mutate",operation:$op,generation:$state.result.generation,revision:$state.result.revision}')
    api "$request" "$saved"
}

@test "first API run returns discover sessions and resumed run returns the current generation" {
    run api '{"command":"run","pr":"https://github.com/org/repo/pull/7"}' ''
    assert_success
    saved=$(session_directory)
    jq -se --arg saved "$saved" 'last.result.sessions | length==1 and
        .[0].session==$saved and .[0].pr==7 and .[0].repository=="github.com/org/repo"' <<< "$output"
    generation=$(jq -sr 'last.result.sessions[0].generation' <<< "$output")
    stages=$(cat "$FIXTURE/stages")
    run api '{"command":"read"}' "$saved"; assert_success
    jq -e --arg generation "$generation" '.result.generation==$generation and (.result.comments|length)>0' <<< "$output"
    run api '{"command":"run"}' "$saved"; assert_success
    jq -se --arg saved "$saved" --arg generation "$generation" 'last.result |
        .session==$saved and .generation==$generation and .new_review_ready==true' <<< "$output"
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}

@test "incomplete first API run reports paused state and resumes without repeating reviews" {
    export MOCK_GRAPHQL_FAIL=1
    run api '{"command":"run","pr":7}' ''; assert_success
    jq -se 'last.result | .new_review_ready==false and .status=="paused" and (.sessions|length)==1' <<< "$output"
    saved=$(session_directory)
    manifest_is '.status=="paused" and .publication.status=="paused"'
    unset MOCK_GRAPHQL_FAIL
    run api '{"command":"run"}' "$saved"; assert_success
    jq -se 'last.result.new_review_ready==true' <<< "$output"
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "paused API replacement reports current generation until discussion and matching complete" {
    run_review; assert_success
    saved=$(session_directory)
    api_mutate edit '{"comment":"p1","body":"Retained current draft"}' > /dev/null
    state=$(api '{"command":"read"}' "$saved")
    generation=$(jq -r .result.generation <<< "$state")
    for failure in MOCK_GRAPHQL_FAIL MOCK_MATCH_CONFLICT; do
        if [[ $failure == MOCK_GRAPHQL_FAIL ]]; then export MOCK_GRAPHQL_FAIL=1
        else unset MOCK_GRAPHQL_FAIL; export MOCK_MATCH_CONFLICT=always; fi
        run api '{"command":"run","synthesis_tests":"full"}' "$saved"; assert_success
        jq -se --arg generation "$generation" 'last.result | .generation==$generation and
            .new_review_ready==false and .status=="paused" and .pending_generation!=$generation' <<< "$output"
        manifest_is '.status=="paused"'
        state=$(api '{"command":"read"}' "$saved")
        jq -e --arg generation "$generation" '.result.generation==$generation and .result.comments[0].body=="Retained current draft"' <<< "$state"
    done
    unset MOCK_MATCH_CONFLICT
    run api '{"command":"run"}' "$saved"; assert_success
    jq -se --arg generation "$generation" 'last.result | .new_review_ready==true and .generation!=$generation' <<< "$output"
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
}

@test "API adopts legacy drafts without model calls and posts separate inline comments" {
    run_review; assert_success
    saved=$(session_directory)
    before=$(cat "$FIXTURE/stages")
    api_mutate placement '{"comment":"p1","placement":{"kind":"inline","path":"source.txt","side":"RIGHT","line":2}}' > /dev/null
    api_mutate edit '{"comment":"p1","body":"Exact human text"}' > /dev/null
    api_mutate stage '{"comment":"p1"}' > /dev/null
    state=$(api '{"command":"read"}' "$saved")
    request=$(jq -c '.result|{command:"prepare",generation,revision}' <<< "$state")
    run api "$request" "$saved"; assert_success
    jq -e '.result.ready' <<< "$output"
    request=$(jq -c '.result|{command:"publish",confirmation,generation:.review.generation,revision:.review.revision}' <<< "$output")
    run api "$request" "$saved"; assert_success
    jq -e '.result.complete and .result.review.comments[0].status=="published"' <<< "$output"
    jq -e '.comments|length==1' "$FIXTURE/api-payload.json"
    jq -e '.comments[0].body|startswith("Exact human text\n\n<!--")' "$FIXTURE/api-payload.json"
    [ "$before" = "$(cat "$FIXTURE/stages")" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "reply wrapper reviews do not invalidate a confirmed inline batch" {
    discussion_fixture ADDITION
    add_new_finding
    run_review; assert_success
    saved=$(session_directory)
    api_mutate placement '{"comment":"p2","placement":{"kind":"inline","path":"source.txt","side":"RIGHT","line":2}}' > /dev/null
    api_mutate stage '{"comments":["p1","p2"]}' > /dev/null
    state=$(api '{"command":"read"}' "$saved")
    request=$(jq -c '.result|{command:"prepare",generation,revision}' <<< "$state")
    run api "$request" "$saved"; assert_success
    request=$(jq -c '.result|{command:"publish",confirmation,generation:.review.generation,revision:.review.revision}' <<< "$output")
    run api "$request" "$saved"; assert_success
    jq -e '.result.complete and all(.result.review.comments[]; .status=="published")' <<< "$output"
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "rematching after published replies and inline comments establishes a stable new baseline" {
    discussion_fixture ADDITION
    add_new_finding
    run_review; assert_success
    saved=$(session_directory)
    api_mutate placement '{"comment":"p2","placement":{"kind":"inline","path":"source.txt","side":"RIGHT","line":2}}' > /dev/null
    api_mutate stage '{"comments":["p1","p2"]}' > /dev/null
    state=$(api '{"command":"read"}' "$saved")
    generation=$(jq -r .result.generation <<< "$state")
    request=$(jq -c '.result|{command:"prepare",generation,revision}' <<< "$state")
    run api "$request" "$saved"; assert_success
    request=$(jq -c '.result|{command:"publish",confirmation,generation:.review.generation,revision:.review.revision}' <<< "$output")
    run api "$request" "$saved"; assert_success
    history_hash=$(sha256sum "$saved/publication/drafts.json")
    jq '.[1].body += " New external context."' "$FIXTURE/inline.json" > "$FIXTURE/new-inline.json"
    mv "$FIXTURE/new-inline.json" "$FIXTURE/inline.json"
    run_review; assert_success
    candidate=$(run_directory)
    [ "$candidate" != "$saved" ]
    stages=$(cat "$FIXTURE/stages")
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    [ "$(run_directory)" = "$candidate" ]
    manifest_is '.publication.published_actions==0'
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$history_hash" ]
    request=$(jq -cn --arg generation "$generation" '{command:"read",generation:$generation}')
    state=$(api "$request" "$saved")
    jq -e '.result.comments | all(.status=="published" and .receipt.id!=null)' <<< "$state"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
}

@test "API operation IDs cancel the owned model process without losing checkpoints" {
    export MOCK_SLEEP_STAGE=claude MOCK_SLEEP_SECONDS=30
    run python3 "$PROJECT_ROOT/tests/helpers/api-cancel.py" "$CLI"
    assert_success
    manifest_is '.phases.claude.status=="interrupted" and .phases.claude.exit_status==143'
    unset MOCK_SLEEP_STAGE MOCK_SLEEP_SECONDS
    run_review; assert_success
}

@test "existing blocked replacement resumes after API publication without reusing receipts" {
    run_review; assert_success
    saved=$(session_directory)
    api_mutate placement '{"comment":"p1","placement":{"kind":"inline","path":"source.txt","side":"RIGHT","line":2}}' > /dev/null
    api_mutate edit '{"comment":"p1","body":"Carefully edited and posted wording"}' > /dev/null
    api_mutate stage '{"comment":"p1"}' > /dev/null
    state=$(api '{"command":"read"}' "$saved")
    generation=$(jq -r .result.generation <<< "$state")
    request=$(jq -c '.result|{command:"prepare",generation,revision}' <<< "$state")
    run api "$request" "$saved"; assert_success
    request=$(jq -c '.result|{command:"publish",confirmation,generation:.review.generation,revision:.review.revision}' <<< "$output")
    run api "$request" "$saved"; assert_success
    hashes=$(sha256sum "$saved/publication/drafts.json")
    # Recreate the attempt left by the old guard, including copied API receipts.
    candidate=$saved-history/attempt-blocked
    mkdir -p "$candidate"
    cp -a "$saved/manifest.json" "$saved/context" "$saved/prompts" "$saved/reviews" "$saved/publication" "$saved/logs" "$saved/discussion" "$candidate/"
    jq --arg saved "$saved" '.session_directory=$saved | .attempt={status:"building",previous:$saved} |
        .configuration.claude_model="haiku" | .configuration.processing_provider="codex"' "$candidate/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$candidate/manifest.json"
    ln -s "$candidate" "$saved/.pending-review"
    export MOCK_FAIL_STAGE=claude
    run_review; assert_failure
    [[ $output != *'Review inputs changed after publication'* && $output != *'Partially published'* ]]
    [ "$(run_directory)" = "$candidate" ]
    [ ! -L "$saved/.current-review" ]
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$hashes" ]
    jq -e 'length==0' "$candidate/publication/actions.json"
    manifest_is '.publication.published_actions==0 and .publication_reset.status=="complete"'
    # Resume also completes an interrupted ledger reset, without changing its ID.
    new_id=$(jq -r .run_id "$candidate/manifest.json")
    jq '.publication_reset.status="pending"' "$candidate/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$candidate/manifest.json"
    cp "$saved/publication/actions.json" "$candidate/publication/actions.json"
    cp "$saved/publication/drafts.json" "$candidate/publication/drafts.json"
    # An uncertain post made in the still-current review must block the candidate.
    jq '.batches[0].actions[0].status="ambiguous"' "$saved/publication/drafts.json" > "$FIXTURE/ambiguous.tmp"
    cp "$saved/publication/drafts.json" "$FIXTURE/original-drafts.json"
    mv "$FIXTURE/ambiguous.tmp" "$saved/publication/drafts.json"
    stages=$(cat "$FIXTURE/stages")
    run_review; assert_failure
    [[ $output == *'Resolve uncertain publication'* ]]
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    cp "$FIXTURE/original-drafts.json" "$saved/publication/drafts.json"
    unset MOCK_FAIL_STAGE
    printf '%s\n' '[{"id":"F1","title":"A different finding","validity":"CONFIRMED","evidence":"source.txt:2 changed behavior","blocking":false,"body_md":"New issue, despite reusing F1."}]' > "$FIXTURE/findings.json"
    run_review; assert_success
    [ "$(realpath -e "$saved/.current-review")" = "$candidate" ]
    [ "$(jq -r .run_id "$candidate/manifest.json")" = "$new_id" ]
    state=$(api '{"command":"read"}' "$saved")
    jq -e '.result.comments[0].status=="unstaged" and .result.comments[0].receipt==null and .result.batches==[]' <<< "$state"
    request=$(jq -cn --arg generation "$generation" '{command:"read",generation:$generation}')
    state=$(api "$request" "$saved")
    jq -e '.result.historical and .result.comments[0].body=="Carefully edited and posted wording" and
        .result.comments[0].status=="published" and (.result.comments[0].receipt.html_url|length>0)' <<< "$state"
    discussion=$(jq -r .discussion.path "$candidate/manifest.json")
    jq -e 'any(.items[].comments[]; .body|contains("Carefully edited and posted wording"))' "$discussion"
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$hashes" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    stages=$(cat "$FIXTURE/stages")
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}
