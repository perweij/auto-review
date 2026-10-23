#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

invalidate_claude() {
    jq '.phases.claude.fingerprint="older-script-version"' "$saved/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$saved/manifest.json"
}

@test "invalid cache record after failed synthesis archives both reviews and restarts automatically" {
    export MOCK_FAIL_STAGE=synthesis
    run_review; assert_failure
    saved=$(run_directory)
    prior=$(cat "$saved/reviews/codex.md")
    invalidate_claude
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [[ $output == *'Restarting reviews from claude'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nclaude\ncodex\nsynthesis\ncontributions' ]
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ "$(cat "$archive/reviews/codex.md")" = "$prior" ]
    [ -s "$archive/reviews/claude-independent.md" ]
    [ -f "$archive/logs/synthesis.stderr.log" ]
    manifest_is '.review_restart==null and .phases.synthesis.status=="completed"'
    run_review; assert_success
    manifest_is '.review_history|length==1'
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "changing only Codex retains Claude but invalidates synthesis and matching" {
    run_review; assert_success
    saved=$(run_directory)
    claude_hash=$(sha256sum "$saved/reviews/claude-independent.md")
    run_review --codex-model other-codex; assert_success
    [[ $output == *'Restarting reviews from codex'* && $output == *'Reusing completed Claude review'* ]]
    [ "$(sha256sum "$saved/reviews/claude-independent.md")" = "$claude_hash" ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\ncodex\nsynthesis\ncontributions' ]
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ -s "$archive/reviews/claude-independent.md" ]
    [ -s "$archive/reviews/codex.md" ]
    [ -s "$archive/reviews/synthesis.json" ]
}

@test "damaged Claude report restarts both reviewers and retains human draft history" {
    run_review; assert_success
    saved=$(run_directory)
    echo 'Human edited body' > "$saved/publication/review-body.md"
    echo 'damaged report' > "$saved/reviews/claude-independent.md"
    run_review; assert_success
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ "$(cat "$archive/reviews/claude-independent.md")" = 'damaged report' ]
    [ "$(cat "$archive/publication/review-body.md")" = 'Human edited body' ]
    [ "$(cat "$saved/publication/review-body.md")" = 'Human edited body' ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "resume finishes a partially moved restart before running independent reviewers" {
    run_review; assert_success
    saved=$(run_directory)
    archive=$saved-history/interrupted
    mkdir -p "$archive/reviews"
    # Simulate interruption after moving the Codex report but before invalidating
    # the phase records or moving the remaining files.
    jq --arg archive "$archive" --arg saved "$saved" '.review_restart={from:"claude",archive:$archive,
        files:[$saved+"/reviews/codex.md",$saved+"/reviews/claude-independent.md",$saved+"/reviews/synthesis.json",$saved+"/reviews/contributions.json"]}' \
        "$saved/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$saved/manifest.json"
    mv "$saved/reviews/codex.md" "$archive/reviews/codex.md"
    run_review; assert_success
    [ -s "$archive/reviews/codex.md" ]
    [ -s "$archive/reviews/claude-independent.md" ]
    manifest_is '.review_restart==null and (.review_history|length)==1 and .phases.claude.status=="completed"'
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "restart preserves publication records and stops after any publication attempt" {
    tty_review 'c|yes'; assert_success
    saved=$(run_directory)
    actions=$(sha256sum "$saved/publication/actions.json")
    invalidate_claude
    run_review; assert_failure
    [[ $output == *'Review inputs changed after publication was attempted'* ]]
    [ "$(sha256sum "$saved/publication/actions.json")" = "$actions" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    manifest_is '.review_restart==null and .review_history==null'
    [ -s "$saved/reviews/codex.md" ]
}
