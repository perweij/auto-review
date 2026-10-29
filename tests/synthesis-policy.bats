#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

assert_focused_independent_reviews() {
    local saved=$1
    jq -e 'all(.phases.claude,.phases.codex; .testing_policy=="focused")' "$saved/manifest.json"
    jq -e --rawfile policy "$PROJECT_ROOT/prompts/independent.txt" '
        [.[]|select(.stage=="claude")][-1].argv as $claude |
        [.[]|select(.stage=="codex")][-1].argv as $codex |
        $claude[$claude|index("--append-system-prompt")+1]==($policy|rtrimstr("\n")) and
        ([$codex[]|select(startswith("developer_instructions="))|ltrimstr("developer_instructions=")|fromjson]==[$policy]) and
        $codex[$codex|index("--base")+1]=="refs/pr-review/7/base" and
        $codex[1:3]==["exec","review"]' "$saved/logs/commands.json"
}

@test "focused policy reaches both independent reviewers and synthesis while resume reuses all stages" {
    run_review --claude-model haiku --codex-model gpt-6-luna --processing-provider claude; assert_success
    saved=$(run_directory)
    manifest_is '.phases.synthesis.testing_policy=="focused"'
    assert_focused_independent_reviews "$saved"
    manifest_is '.phases.claude.model=="haiku" and .phases.codex.model=="gpt-6-luna"'
    jq -e --rawfile policy "$PROJECT_ROOT/prompts/synthesis-testing-focused.txt" '
        [.[]|select(.stage=="synthesis")][-1].argv as $args |
        $args[$args|index("--append-system-prompt")+1]==($policy|rtrimstr("\n"))' "$saved/logs/commands.json"
    run_review; assert_success
    [[ $output == *'Reusing completed synthesis'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "full-suite opt-in changes synthesis only and preserves completed drafts through failure" {
    run_review --processing-provider claude; assert_success
    previous=$(run_directory)
    printf 'Human wording worth preserving\n' > "$previous/publication/review-body.md"
    hashes=$(sha256sum "$previous/reviews/synthesis.json" "$previous/publication/review-body.md")
    export MOCK_FAIL_STAGE=synthesis
    run_review --synthesis-tests full; assert_failure
    candidate=$(run_directory)
    assert_focused_independent_reviews "$candidate"
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

@test "Codex synthesis full-suite permission does not reach either independent reviewer" {
    run_review --processing-provider codex --synthesis-tests full; assert_success
    saved=$(run_directory)
    assert_focused_independent_reviews "$saved"
    manifest_is '.phases.synthesis.testing_policy=="full"'
    [[ $(cat "$FIXTURE/synthesis-stdin") == *'Synthesis verification policy: full'* ]]
    jq -e '[.[]|select(.stage=="synthesis")][-1].argv | .[0]=="codex" and index("--output-schema")!=null' "$saved/logs/commands.json"
}

@test "invalid synthesis policy is rejected before authentication or model calls" {
    run_review --synthesis-tests all
    assert_failure
    [[ $output == *'--synthesis-tests must be focused or full'* ]]
    [ ! -e "$FIXTURE/calls.jsonl" ]
    run_review --synthesis-tests
    assert_failure
    [[ $output == *'expected one argument'* ]]
    [ ! -e "$FIXTURE/calls.jsonl" ]
}
