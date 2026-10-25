#!/usr/bin/env bats
load helpers/fixture

@test "follow-up checks latest head with one provider and preserves previous drafts" {
    discussion_fixture
    run_review
    assert_success
    local session previous
    session=$(session_directory)
    previous=$(run_directory)
    printf 'fixed\n' >> source.txt
    git commit -qam fix
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    run_review --resume "$session" --followup-thread thread:T1
    assert_success
    manifest_is '.analysis_kind=="followup" and .phases.followup.status=="completed" and .phases.synthesis==null'
    [ -f "$previous/reviews/synthesis.json" ]
    [ "$(tail -n1 "$FIXTURE/stages")" = followup ]
    [ "$(grep -c '^synthesis$' "$FIXTURE/stages")" = 1 ]
    jq -e '.comments==[] and .recommendation==null and .verdict=="COMMENT"' "$(run_directory)/publication/drafts.json"
    run_review --resume "$session" --followup-thread thread:T1
    assert_success
    [ "$(grep -c '^followup$' "$FIXTURE/stages")" = 1 ]
}

@test "failed follow-up retains current full review and resumes" {
    discussion_fixture
    run_review
    assert_success
    local session original
    session=$(session_directory)
    original=$(run_directory)
    export MOCK_FAIL_STAGE=followup
    run_review --resume "$session" --followup-thread thread:T1
    assert_failure
    [ ! -e "$session/.current-review" ]
    [ -f "$original/reviews/synthesis.json" ]
    unset MOCK_FAIL_STAGE
    run_review --resume "$session" --followup-thread thread:T1
    assert_success
    [ -L "$session/.current-review" ]
    run_review --resume "$session"
    assert_success
    manifest_is '.analysis_kind=="full" and .phases.synthesis.status=="completed"'
}

@test "follow-up uses the configured Codex processor and rejects missing threads" {
    discussion_fixture
    run_review --processing-provider codex
    assert_success
    run_review --followup-thread thread:missing
    assert_failure
    [[ $output == *'selected conversation is missing'* ]]
    run_review --followup-thread thread:T1
    assert_success
    manifest_is '.analysis_kind=="followup" and .configuration.processing_provider=="codex"'
    [ "$(grep -c '^followup$' "$FIXTURE/stages")" = 1 ]
}

@test "follow-up API cancellation preserves the current full review and resumes" {
    discussion_fixture
    run_review
    assert_success
    local session
    session=$(session_directory)
    jq -nc --arg session "$session" '{protocol:1,command:"read",session:$session}' | "$CLI" api > "$FIXTURE/view.json"
    jq '.result | {protocol:1,command:"followup",session,generation,revision,threads:["thread:T1"]}' "$FIXTURE/view.json" > "$FIXTURE/request.json"
    export MOCK_SLEEP_STAGE=followup MOCK_SLEEP_SECONDS=30
    run python3 "$PROJECT_ROOT/tests/helpers/api-cancel.py" "$CLI" "$FIXTURE/request.json"
    assert_success
    [ ! -e "$session/.current-review" ]
    manifest_is '.phases.followup.status=="interrupted"'
    unset MOCK_SLEEP_STAGE MOCK_SLEEP_SECONDS
    run_review --followup-thread thread:T1
    assert_success
    manifest_is '.phases.followup.status=="completed"'
}

@test "thread-state API resolves and reopens through GraphQL without submitting a review" {
    discussion_fixture
    jq 'map(.viewerCanResolve=true | .viewerCanUnresolve=true)' "$FIXTURE/threads.json" > "$FIXTURE/threads.tmp"
    mv "$FIXTURE/threads.tmp" "$FIXTURE/threads.json"
    run_review
    assert_success
    local session request
    session=$(session_directory)
    jq -nc --arg session "$session" '{protocol:1,command:"read",session:$session}' | "$CLI" api > "$FIXTURE/view.json"
    for resolved in true false; do
        request=$(jq -c --argjson resolved "$resolved" '.result | {protocol:1,command:"thread-state",session,generation,revision,thread:"thread:T1",resolved:$resolved,expected:.thread_contexts["thread:T1"].expected}' "$FIXTURE/view.json")
        run bash -c 'printf "%s\n" "$1" | "$2" api' -- "$request" "$CLI"
        assert_success
        jq -e --argjson resolved "$resolved" '.result.changed and .result.review.thread_contexts["thread:T1"].thread.is_resolved==$resolved' <<< "$output"
        jq '{result:.result.review}' <<< "$output" > "$FIXTURE/view.json"
    done
    [ ! -f "$FIXTURE/post-count" ]
    [ ! -f "$FIXTURE/reply-count" ]
}
