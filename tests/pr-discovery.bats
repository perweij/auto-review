#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "merged and closed PRs review the recorded base after main advances" {
    export MOCK_PR_BASE_SHA=$BASE
    git commit --allow-empty -qm 'Main after merge'
    git push -q "$FIXTURE/remote.git" HEAD:refs/heads/main
    for state in MERGED CLOSED; do
        export MOCK_PR_STATE=$state
        run_review; assert_success
        [[ $output != *'PR refs changed during setup'* ]]
        [ "$(git rev-parse refs/pr-review/7/base)" = "$BASE" ]
        [ "$(jq -r .snapshot.merge_base "$(run_directory)/manifest.json")" = "$BASE" ]
        [ -s "$(run_directory)/context/pr.diff" ]
        [ ! -f "$FIXTURE/post-count" ]
    done
}

@test "open PRs use the live base branch when PR metadata retains an older base" {
    export MOCK_PR_BASE_SHA=$BASE
    git commit --allow-empty -qm 'Main advanced after PR creation'
    git push -q "$FIXTURE/remote.git" HEAD:refs/heads/main
    run_review; assert_success
    [[ $output != *'PR refs changed during setup'* ]]
    [ "$(git rev-parse refs/pr-review/7/base)" = "$(git rev-parse HEAD)" ]
    [ "$(jq -r .snapshot.base "$(run_directory)/manifest.json")" = "$(git rev-parse HEAD)" ]
    [ ! -f "$FIXTURE/post-count" ]
}
