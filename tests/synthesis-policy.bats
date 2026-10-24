#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "focused synthesis policy is passed to Claude and ordinary resume reuses it" {
    run_review; assert_success
    saved=$(run_directory)
    manifest_is '.phases.synthesis.testing_policy=="focused"'
    jq -e --rawfile policy "$PROJECT_ROOT/prompts/synthesis-testing-focused.txt" '
        [.[]|select(.stage=="synthesis")][-1].argv as $args |
        $args[$args|index("--append-system-prompt")+1]==($policy|rtrimstr("\n"))' "$saved/logs/commands.json"
    run_review; assert_success
    [[ $output == *'Reusing completed synthesis'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "full-suite opt-in changes synthesis only and preserves completed drafts through failure" {
    run_review; assert_success
    previous=$(run_directory)
    printf 'Human wording worth preserving\n' > "$previous/publication/review-body.md"
    hashes=$(sha256sum "$previous/reviews/synthesis.json" "$previous/publication/review-body.md")
    export MOCK_FAIL_STAGE=synthesis
    run_review --synthesis-tests full; assert_failure
    candidate=$(run_directory)
    [ "$candidate" != "$previous" ]
    [ ! -L "$previous/.current-review" ]
    [ "$(sha256sum "$previous/reviews/synthesis.json" "$previous/publication/review-body.md")" = "$hashes" ]
    manifest_is '.phases.synthesis.testing_policy=="full" and .phases.synthesis.status=="failed"'
    jq -e --rawfile policy "$PROJECT_ROOT/prompts/synthesis-testing-full.txt" '
        [.[]|select(.stage=="synthesis")][-1].argv as $args |
        $args[$args|index("--append-system-prompt")+1]==($policy|rtrimstr("\n"))' "$candidate/logs/commands.json"
    unset MOCK_FAIL_STAGE
    run_review --synthesis-tests full; assert_success
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ "$(sha256sum "$previous/reviews/synthesis.json" "$previous/publication/review-body.md")" = "$hashes" ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\nsynthesis\ncontributions' ]
}

@test "invalid synthesis policy is rejected before authentication or model calls" {
    run_review --synthesis-tests all
    assert_failure
    [[ $output == *'--synthesis-tests must be focused or full'* ]]
    [ ! -e "$FIXTURE/calls.jsonl" ]
    run_review --synthesis-tests
    assert_failure
    [[ $output == *'Missing value for --synthesis-tests'* ]]
    [ ! -e "$FIXTURE/calls.jsonl" ]
}
