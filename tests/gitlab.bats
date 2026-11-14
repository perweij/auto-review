#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

prepare_gitlab() {
    git push -q "$FIXTURE/remote.git" HEAD:refs/merge-requests/7/head
    git remote set-url upstream https://gitlab.com/group/Sub/repo.git
    git config --global url."$FIXTURE/remote.git".insteadOf https://gitlab.com/group/Sub/repo.git
    ln -s "$PROJECT_ROOT/tests/helpers/mock-glab.py" "$FIXTURE/bin/glab"
    rm "$FIXTURE/bin/gh"
    printf '#!/bin/sh\nexit 99\n' > "$FIXTURE/bin/gh"
    chmod +x "$FIXTURE/bin/gh"
}

@test "GitLab completes and resumes local review without invoking gh" {
    prepare_gitlab
    run "$CLI" https://gitlab.com/group/Sub/repo/-/merge_requests/7 --forge gitlab
    assert_success
    manifest_is '.status == "complete" and .forge_project.forge == "gitlab"'
    jq -e '.change.diff_refs.head_sha == .change.source.head' "$(run_directory)/context/review-context.json"
    [[ $output == *'Checkout: '* ]]
    run "$CLI" 7 --resume "$(session_directory)"
    assert_success
    [[ $output == *'Checkout: '* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
    jq -se 'all(.[]; .tool != "gh")' "$FIXTURE/calls.jsonl"
    [ ! -e "$FIXTURE/post-count" ]
}

@test "GitLab authentication and publication fail before model work" {
    prepare_gitlab
    run "$CLI" 7 --forge gitlab --auto-publish
    assert_failure
    [[ $output == *'does not support publication'* ]]
    export MOCK_GLAB_AUTH_FAIL=1
    run "$CLI" 7 --forge gitlab
    assert_failure
    [[ $output == *'authentication failed'* ]]
    [ ! -e "$FIXTURE/stages" ]
}

@test "GitLab incomplete discussion retains local synthesis and pauses" {
    prepare_gitlab
    export MOCK_GLAB_DISCUSSION_FAIL=1
    run "$CLI" 7 --forge gitlab
    [ -f "$(run_directory)/reviews/synthesis.json" ]
    manifest_is '.status == "paused"'
    [ ! -e "$FIXTURE/post-count" ]
}

call_gitlab_api() {
    printf '%s\n' "$1" | "$CLI" api
}

@test "GitLab API accepts MR URLs and discovers the new session" {
    prepare_gitlab
    run call_gitlab_api '{"protocol":1,"command":"run","forge":"gitlab","pr":"https://gitlab.com/GROUP/sub/repo/-/merge_requests/7"}'
    assert_success
    jq -se 'last.result | .new_review_ready and (.sessions|length)==1' <<< "$output"
    saved=$(session_directory)
    run call_gitlab_api "$(jq -nc --arg session "$saved" '{protocol:1,command:"read",session:$session}')"
    assert_success
    jq -e '.result.verdicts == []' <<< "$output"
}
