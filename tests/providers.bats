#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "Codex alone needs no Claude calls and resumes its structured review" {
    export MOCK_AUTH_FAIL=claude
    run_review --reviewers codex --codex-model gpt-6-luna --codex-effort medium
    assert_success
    manifest_is '.configuration.reviewers==["codex"] and .configuration.processing_provider=="codex" and .phases.claude==null and .phases.codex==null'
    jq -se 'all(.[]; .tool!="claude")' "$FIXTURE/calls.jsonl"
    jq -se '[.[]|select(.tool=="codex" and (.args|index("--output-schema")))] | length==2 and all(.[]; .args|index("model_reasoning_effort=medium"))' "$FIXTURE/calls.jsonl"
    [[ $(cat "$FIXTURE/stages") == $'synthesis\ncontributions' ]]
    [[ $output != *'adversarial synthesis'* ]]
    [[ $(cat "$FIXTURE/synthesis-stdin") != *'codex_review'* ]]
    run_review; assert_success
    [[ $(wc -l < "$FIXTURE/stages") == 2 ]]
    export MOCK_ADAPT=1
    printf 'Full thread context and new aspect\n' > "$FIXTURE/adapt-input"
    run bash "$PROJECT_ROOT/lib/api-bridge.sh" adapt "$(run_directory)" COMMENT "$FIXTURE/adapt-input"
    assert_success
    jq -se 'last | .tool=="codex" and (.args|index("gpt-6-luna")) and (.args|index("model_reasoning_effort=medium"))' "$FIXTURE/calls.jsonl"
    cmp "$FIXTURE/adapt-input" "$FIXTURE/adapt-stdin"
}

@test "Claude alone needs no Codex calls and inherits model and effort everywhere" {
    export MOCK_AUTH_FAIL=codex
    run_review --reviewers claude --claude-model haiku --claude-effort low --synthesis-tests full
    assert_success
    jq -se 'all(.[]; .tool!="codex")' "$FIXTURE/calls.jsonl"
    jq -se '[.[]|select(.tool=="claude" and (.args|index("--json-schema")))] | length==2 and all(.[]; (.args|index("haiku")) and (.args|index("low")))' "$FIXTURE/calls.jsonl"
    manifest_is '.configuration.processing_provider=="claude" and .phases.synthesis.testing_policy=="full"'
    [[ $(cat "$FIXTURE/stages") == $'synthesis\ncontributions' ]]
}

@test "switching processing provider reuses independent reports and preserves old configuration" {
    run_review; assert_success
    previous=$(run_directory)
    printf 'Carefully edited text\n' > "$previous/publication/review-body.md"
    export MOCK_FAIL_STAGE=synthesis
    run_review --processing-provider codex; assert_failure
    candidate=$(run_directory)
    [[ $candidate != "$previous" ]]
    jq -e '.configuration.processing_provider=="claude"' "$previous/manifest.json"
    [[ $(cat "$previous/publication/review-body.md") == 'Carefully edited text' ]]
    [[ $(cat "$FIXTURE/stages") == $'claude\ncodex\nsynthesis\ncontributions\nsynthesis' ]]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    manifest_is '.configuration.processing_provider=="codex" and .attempt.status=="ready"'
    [[ $(cat "$FIXTURE/stages") == $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\nsynthesis\ncontributions' ]]
    run_review; assert_success
    [[ $(wc -l < "$FIXTURE/stages") == 7 ]]
}

@test "dual reviewer effort is explicit in both Claude effort controls" {
    run_review --claude-effort medium --codex-effort low --processing-provider codex
    assert_success
    jq -se 'any(.[]; .tool=="claude" and (.args|index("/code-review medium --max-findings default refs/pr-review/7/base")) and (.args|index("medium")))' "$FIXTURE/calls.jsonl"
    jq -se '[.[]|select(.tool=="codex" and (.args|index("--output-schema")))] | length==2 and all(.[]; .args|index("model_reasoning_effort=low"))' "$FIXTURE/calls.jsonl"
}

@test "single provider reassessment stays with that provider" {
    export MOCK_MATCH_CONFLICT=once
    run_review --reviewers codex; assert_success
    jq -se 'all(.[]; .tool!="claude")' "$FIXTURE/calls.jsonl"
    [[ $(cat "$FIXTURE/stages") == $'synthesis\ncontributions\nsynthesis\ncontributions' ]]
}
