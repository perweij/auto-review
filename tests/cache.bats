#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

copy_tool() {
    mkdir -p "$FIXTURE/tool"
    cp -a "$PROJECT_ROOT/bin" "$PROJECT_ROOT/lib" "$PROJECT_ROOT/prompts" "$FIXTURE/tool/"
    CLI=$FIXTURE/tool/bin/pr-review
}

legacy_cache_records() {
    jq '.phases |= with_entries(.value |= (del(.inputs) | .fingerprint="legacy-installation-hash"))' "$saved/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$saved/manifest.json"
}

@test "main draft edits, UI changes and runtime limits reuse every completed model stage" {
    copy_tool
    run_review --claude-model claude-haiku-5-5 --codex-model gpt-6-luna; assert_success
    saved=$(run_directory)
    stages=$(cat "$FIXTURE/stages")
    printf 'My edited main review\n' > "$saved/publication/review-body.md"
    printf '\n# A publication UI change\n' >> "$FIXTURE/tool/lib/publication.sh"
    printf '\n# An unrelated entry point change\n' >> "$FIXTURE/tool/bin/pr-review"
    export PR_REVIEW_MODEL_TIMEOUT=120 PR_REVIEW_CLAUDE_BUDGET=5
    run_review --claude-model claude-haiku-5-5 --codex-model gpt-6-luna; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    [ "$(cat "$saved/publication/review-body.md")" = 'My edited main review' ]
    manifest_is '.review_history==null and .phases.claude.inputs.version==1'
    [[ $output == *'Reusing completed Claude review'* && $output == *'Reusing completed Codex review'* && $output == *'Reusing completed synthesis'* ]]
}

@test "legacy completed reviews upgrade without calls or loss of human edits" {
    run_review; assert_success
    saved=$(run_directory)
    stages=$(cat "$FIXTURE/stages")
    legacy_cache_records
    printf 'Human edited body\n' > "$saved/publication/review-body.md"
    export PR_REVIEW_CLAUDE_BUDGET=5
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    [ "$(cat "$saved/publication/review-body.md")" = 'Human edited body' ]
    manifest_is 'all(.phases | to_entries[] | select(.key=="claude" or .key=="codex" or .key=="synthesis" or .key=="contributions"); .value.inputs.version==1) and .review_history==null'
    [[ $output == *'Verified saved contributions inputs'* ]]
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}

@test "legacy cache still rejects a different model" {
    run_review; assert_success
    saved=$(run_directory)
    legacy_cache_records
    run_review --codex-model other-codex; assert_success
    [[ $output == *'Reusing completed Claude review'* && $output == *'Restarting reviews from codex'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\ncodex\nsynthesis\ncontributions' ]
}

@test "changing the independent prompt restarts reviewers" {
    copy_tool
    run_review; assert_success
    printf '\nCheck retry behavior.\n' >> "$FIXTURE/tool/prompts/independent.txt"
    run_review; assert_success
    [[ $output == *'Restarting reviews from claude'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "synthesis prompt changes retain independent reviews" {
    copy_tool
    run_review; assert_success
    printf '\nExplain retry behavior.\n' >> "$FIXTURE/tool/prompts/synthesis.txt"
    run_review; assert_success
    [[ $output == *'Reusing completed Claude review'* && $output == *'Reusing completed Codex review'* ]]
    # The replacement also prepares its discussion-aware drafts before commit.
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\ncontributions' ]
}

@test "legacy synthesis is not adopted after its saved prompt changes" {
    run_review; assert_success
    saved=$(run_directory)
    legacy_cache_records
    printf '\nUnrecorded alteration\n' >> "$saved/prompts/synthesis-prompt.txt"
    run_review; assert_success
    [[ $output == *'Reusing completed Claude review'* && $output == *'Reusing completed Codex review'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\ncontributions' ]
}
