#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

suggestion_fixture() {
    jq -n '[{id:"F1",title:"Arithmetic",validity:"CONFIRMED",evidence:"Inspected source.txt",blocking:false,
      body_md:"Check the arithmetic.",primary_location:{path:"source.txt",side:"RIGHT",start_line:2,line:2},locations:[]}]' > "$FIXTURE/findings.json"
    jq -n '{reason:"A complete local fix.",suggestion:{target:{path:"source.txt",side:"RIGHT",start_line:2,line:2},original:"change\n",replacement:"fixed\n"}}' > "$FIXTURE/suggestions-model.json"
    jq -n '{accepted:true,evidence:"Independently inspected exact replacement.",checks:[],inspection_only_reason:"Text-only fixture."}' > "$FIXTURE/suggestion-verify-model.json"
}

@test "automatic CLI publishes a verified suggestion and repeat is a noop" {
    suggestion_fixture
    run --separate-stderr "$CLI" 7 --auto-publish --json --suggested-changes
    assert_success
    jq -e '.outcome=="published"' <<< "$output"
    jq -e '.event=="COMMENT" and .comments[0].line==2 and (.comments[0].body|contains("```suggestion\nfixed\n```"))' "$FIXTURE/api-payload.json"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    run --separate-stderr "$CLI" 7 --auto-publish --json
    assert_success
    jq -e '.outcome=="noop"' <<< "$output"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "codex processing verifies suggestions and retains them for interactive review" {
    suggestion_fixture
    run_review --suggested-changes --reviewers codex
    assert_success
    draft_is '.comments[0].suggestion.verification.status=="accepted" and .comments[0].status=="unstaged"'
    tty_drafts $'i p1\nq\n'
    assert_success
    [[ $output == *'No executable checks run: Text-only fixture.'* ]]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "automatic API used by Emacs publishes the same verified suggestion" {
    suggestion_fixture
    run bash -c 'printf "%s\n" '\''{"protocol":1,"command":"automatic-review","pr":7,"suggested_changes":true}'\'' | "$1" api' _ "$CLI"
    assert_success
    [[ $output == *'"outcome": "published"'* ]]
    jq -e '.comments[0].body|contains("```suggestion\nfixed\n```")' "$FIXTURE/api-payload.json"
}

@test "verification rejection blocks the entire automatic batch and GUI can remove suggestion" {
    suggestion_fixture
    jq '.accepted=false | .evidence="The fix needs another edit."' "$FIXTURE/suggestion-verify-model.json" > "$FIXTURE/rejected.json"
    mv "$FIXTURE/rejected.json" "$FIXTURE/suggestion-verify-model.json"
    run --separate-stderr "$CLI" 7 --auto-publish --json --suggested-changes
    [ "$status" = 3 ]
    jq -e '.outcome=="needs_attention" and (.affected|length)==1' <<< "$output"
    [ ! -e "$FIXTURE/post-count" ]
    draft_is '.comments[0].suggestion.verification.status=="rejected"'
    tty_drafts $'i p1\nsx p1\ns p1\np\nyes\nq\n'
    assert_success
    [[ $output == *'The fix needs another edit.'* ]]
    jq -e '.comments[0].body|contains("suggestion")|not' "$FIXTURE/api-payload.json"
}

@test "disabled suggestions leave the established pipeline unchanged" {
    suggestion_fixture
    run_review --no-suggested-changes
    assert_success
    draft_is '.comments[0].suggestion==null'
    run grep -q suggestion "$FIXTURE/stages"
    [ "$status" = 1 ]
}
