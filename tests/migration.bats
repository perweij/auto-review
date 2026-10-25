#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "CLI explicitly resumes an external session while API path validation stays restricted" {
    run_review; assert_success
    saved=$(run_directory)
    before=$(cat "$FIXTURE/stages")
    mkdir "$FIXTURE/other-state"
    export XDG_STATE_HOME="$FIXTURE/other-state"
    run_review --resume "$saved"; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$before" ]
    request=$(jq -nc --arg session "$saved" '{protocol:1,command:"read",session:$session}')
    read_request() { printf '%s\n' "$request" | "$CLI" api; }
    run read_request; assert_failure
    [[ $output == *invalid_session* ]]
    tty_drafts 'i p1|q' "$saved"; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$before" ]
}

@test "terminal and API share exact edits staging and receipts without model calls" {
    discussion_fixture
    tty_review 's p1|q'; assert_success
    saved=$(session_directory)
    before=$(cat "$FIXTURE/stages")
    state=$(jq -nc --arg session "$saved" '{protocol:1,command:"read",session:$session}' | "$CLI" api)
    request=$(jq -c '.result|{protocol:1,session,generation,revision,command:"mutate",operation:"edit",comment:"p1",body:"Text edited through Emacs API α\n\n"}' <<< "$state")
    printf '%s\n' "$request" | "$CLI" api > /dev/null
    tty_drafts 'i p1|s p1|p|yes|q'; assert_success
    [[ $output == *'Text edited through Emacs API α'* ]]
    jq -e '.body|startswith("Text edited through Emacs API α\n\n\n\n<!--")' "$FIXTURE/reply-response.json"
    state=$(jq -nc --arg session "$saved" '{protocol:1,command:"read",session:$session}' | "$CLI" api)
    jq -e '.result.comments[0].status=="published" and .result.comments[0].receipt.id==1001' <<< "$state"
    [ "$(cat "$FIXTURE/stages")" = "$before" ]
}
