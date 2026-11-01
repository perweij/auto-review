#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "two Claude slots use independent settings without Codex and reuse an unchanged slot" {
    export MOCK_AUTH_FAIL=codex
    run_review --reviewers claude,claude --processing-provider claude \
        --reviewer-1-model haiku --reviewer-1-effort low --reviewer-2-model sonnet --reviewer-2-effort high
    assert_success
    jq -se 'all(.[]; .tool!="codex")' "$FIXTURE/calls.jsonl"
    manifest_is '.phases["reviewer-1"].model=="haiku" and .phases["reviewer-2"].model=="sonnet"'
    [ -s "$(run_directory)/reviews/reviewer-1.md" ]
    [ -s "$(run_directory)/reviews/reviewer-2.md" ]
    grep -q 'reviewer-1.md' "$FIXTURE/synthesis-stdin"
    grep -q 'reviewer-2.md' "$FIXTURE/synthesis-stdin"
    run_review --reviewers claude,claude --reviewer-2-model opus; assert_success
    [[ $output == *'Reusing completed Reviewer-1 review'* ]]
    manifest_is '.configuration.reviewer_slots[0].model=="haiku" and .configuration.reviewer_slots[1].model=="opus" and .configuration.processing.provider=="claude"'
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 3 ]
}

@test "two Codex slots use independent settings without Claude" {
    export MOCK_AUTH_FAIL=claude
    run_review --reviewers codex,codex --processing-provider codex \
        --reviewer-1-model gpt-6-luna --reviewer-1-effort low --reviewer-2-model gpt-6.1-sol --reviewer-2-effort high
    assert_success
    jq -se 'all(.[]; .tool!="claude")' "$FIXTURE/calls.jsonl"
    manifest_is '.phases["reviewer-1"].model=="gpt-6-luna" and .phases["reviewer-2"].model=="gpt-6.1-sol"'
    [ -s "$(run_directory)/reviews/reviewer-1.md" ]
    [ -s "$(run_directory)/reviews/reviewer-2.md" ]
    grep -q 'reviewer-1.md' "$FIXTURE/synthesis-stdin"
    grep -q 'reviewer-2.md' "$FIXTURE/synthesis-stdin"
    run_review --reviewers codex,codex; assert_success
    [[ $output == *'Reusing completed Reviewer-1 review'* && $output == *'Reusing completed Reviewer-2 review'* ]]
}

@test "single reviewer and separate processor both run with their configured choices" {
    run_review --reviewers claude --reviewer-1-model haiku --reviewer-1-effort low \
        --processing-provider codex --processing-model gpt-6.1-sol --processing-effort high
    assert_success
    manifest_is '.configuration.reviewer_slots[0].model=="haiku" and .configuration.processing.model=="gpt-6.1-sol" and .phases["reviewer-1"].status=="completed"'
    [ "$(cat "$FIXTURE/stages")" = $'claude\nsynthesis\ncontributions' ]
    grep -q 'reviewer-1.md' "$FIXTURE/synthesis-stdin"
    grep -q 'Evaluate EVERY' "$FIXTURE/synthesis-stdin"
    grep -q 'for every original finding' "$FIXTURE/synthesis-stdin"
    grep -q 'attribute it to the supplied reviewer slot ID' "$FIXTURE/synthesis-stdin"
    run ! grep -q 'You are the sole reviewer' "$FIXTURE/synthesis-stdin"
    jq -se 'any(.[]; .tool=="claude" and (.args|index("haiku"))) and any(.[]; .tool=="codex" and (.args|index("gpt-6.1-sol")) and (.args|index("--output-schema")))' "$FIXTURE/calls.jsonl"
}

@test "single Codex reviewer can use independent Claude processing" {
    run_review --reviewers codex --reviewer-1-model gpt-6-luna --reviewer-1-effort low \
        --processing-provider claude --processing-model haiku --processing-effort high
    assert_success
    manifest_is '.configuration.reviewer_slots[0].model=="gpt-6-luna" and .configuration.processing.model=="haiku" and .phases["reviewer-1"].status=="completed"'
    [ "$(cat "$FIXTURE/stages")" = $'codex\nsynthesis\ncontributions' ]
    grep -q 'reviewer-1.md' "$FIXTURE/synthesis-stdin"
    grep -q 'Evaluate EVERY' "$FIXTURE/synthesis-stdin"
    grep -q 'for every original finding' "$FIXTURE/synthesis-stdin"
    grep -q 'attribute it to the supplied reviewer slot ID' "$FIXTURE/synthesis-stdin"
    run ! grep -q 'You are the sole reviewer' "$FIXTURE/synthesis-stdin"
    jq -se 'any(.[]; .tool=="codex" and (.args|index("gpt-6-luna")) and (.args|index("review"))) and any(.[]; .tool=="claude" and (.args|index("haiku")) and (.args|index("--json-schema")))' "$FIXTURE/calls.jsonl"
}

@test "Codex alone needs no Claude calls and resumes its structured review" {
    discussion_fixture
    export MOCK_AUTH_FAIL=claude
    run_review --reviewers codex --codex-model gpt-6-luna --codex-effort medium
    assert_success
    manifest_is '.configuration.reviewers==["codex"] and .configuration.processing_provider=="codex" and .phases["reviewer-1"]==null and .phases["reviewer-2"]==null'
    jq -se 'all(.[]; .tool!="claude")' "$FIXTURE/calls.jsonl"
    jq -se '[.[]|select(.tool=="codex" and (.args|index("--output-schema")))] | length==2 and all(.[]; .args|index("model_reasoning_effort=medium"))' "$FIXTURE/calls.jsonl"
    [[ $(cat "$FIXTURE/stages") == $'synthesis\ncontributions' ]]
    [[ $output != *'adversarial synthesis'* ]]
    [[ $(cat "$FIXTURE/synthesis-stdin") != *'codex_review'* ]]
    run_review; assert_success
    [[ $(wc -l < "$FIXTURE/stages") == 2 ]]
    export MOCK_ADAPT=1
    saved=$(run_directory)
    request=$(jq -nc --arg session "$saved" '{protocol:1,command:"read",session:$session}')
    state=$(printf '%s\n' "$request" | "$CLI" api)
    request=$(jq -c '.result | {protocol:1,command:"adapt",session,generation,revision,comment:"p1"}' <<< "$state")
    adapt_request() { printf '%s\n' "$request" | "$CLI" api; }
    run adapt_request
    assert_success
    jq -se 'last | .tool=="codex" and (.args|index("gpt-6-luna")) and (.args|index("model_reasoning_effort=medium"))' "$FIXTURE/calls.jsonl"
    grep -q 'reuses the same key' "$FIXTURE/adapt-stdin"
    [[ $output == *'A contextual reply'* ]]

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
    run_review --processing-provider claude; assert_success
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

@test "economical preset runs both reviewers and Terra processing at high effort" {
    run_review --economical
    assert_success
    manifest_is '.configuration | .reviewers==["claude","codex"] and .claude_model=="haiku" and .codex_model=="gpt-6.1-terra" and .processing_provider=="codex" and .claude_effort=="high" and .codex_effort=="high"'
    jq -se 'any(.[]; .tool=="claude" and (.args|index("haiku"))) and any(.[]; .tool=="codex" and (.args|index("gpt-6.1-terra")) and (.args|index("--output-schema")))' "$FIXTURE/calls.jsonl"
}

@test "economical explicit overrides and saved settings take precedence" {
    run_review --codex-effort medium --economical
    assert_success
    manifest_is '.configuration.codex_effort=="medium" and .configuration.codex_model=="gpt-6.1-terra"'
    count=$(wc -l < "$FIXTURE/stages")
    run_review --economical
    assert_success
    manifest_is '.configuration.codex_effort=="medium"'
    [[ $(wc -l < "$FIXTURE/stages") == "$count" ]]
}

@test "expensive preset runs Fable and Astra with Astra processing at high effort" {
    run_review --expensive
    assert_success
    manifest_is '.configuration | .reviewers==["claude","codex"] and .claude_model=="fable" and .codex_model=="gpt-6-astra" and .processing_provider=="codex" and .claude_effort=="high" and .codex_effort=="high"'
    jq -se 'any(.[]; .tool=="claude" and (.args|index("fable"))) and any(.[]; .tool=="codex" and (.args|index("gpt-6-astra")) and (.args|index("--output-schema")))' "$FIXTURE/calls.jsonl"
}
