#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "live probes use cheap models before reviews and filter credentials" {
    export GH_TOKEN=fixture-secret-github ANTHROPIC_API_KEY=fixture-claude OPENAI_API_KEY=fixture-codex
    run_review; assert_success
    [ "$(cat "$FIXTURE/probes")" = $'claude\ncodex' ]
    manifest_is '.phases.claude_auth_probe.status=="completed" and .phases.claude_auth_probe.model=="haiku" and .phases.codex_auth_probe.model=="gpt-6-luna"'
    jq -e '.[0].stage=="claude_auth_probe" and .[1].stage=="codex_auth_probe" and .[2].stage=="claude"' "$(run_directory)/logs/commands.json"
    run grep -q GH_TOKEN "$FIXTURE/environment-claude-probe"; [ "$status" = 1 ]
    run grep -q OPENAI_API_KEY "$FIXTURE/environment-claude-probe"; [ "$status" = 1 ]
    run grep -q ANTHROPIC_API_KEY "$FIXTURE/environment-codex-probe"; [ "$status" = 1 ]
    grep -q ANTHROPIC_API_KEY "$FIXTURE/environment-claude-probe"
    grep -q OPENAI_API_KEY "$FIXTURE/environment-codex-probe"
}

@test "expired OAuth fails live preflight even when local status and process exit succeed" {
    export MOCK_PROBE_FAIL=claude MOCK_PROBE_EXIT=0
    run_review; assert_failure
    [[ $output == *'claude auth login'* && $output == *'is_error=true'* ]]
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
    [ ! -e "$FIXTURE/stages" ]
    [ "$(cat "$FIXTURE/probes")" = claude ]
    manifest_is '.phases.claude_auth_probe.status=="failed" and .phases.claude_auth_probe.failure_reason=="oauth_session_expired" and .snapshot==null'
    [ -s "$(run_directory)/logs/claude_auth_probe.jsonl" ]
    [ ! -s "$(run_directory)/logs/claude_auth_probe.stderr.log" ]
    unset MOCK_PROBE_FAIL MOCK_PROBE_EXIT
    run_review --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/probes")" = $'claude\nclaude\ncodex' ]
}

@test "Codex auth failure stops before checkout and reports login recovery" {
    export MOCK_PROBE_FAIL=codex
    run_review; assert_failure
    [[ $output == *'codex login'* ]]
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
    [ ! -e "$FIXTURE/stages" ]
    manifest_is '.phases.codex_auth_probe.failure_reason=="authentication" and .phases.codex_auth_probe.exit_status==1'
}

@test "probe model access and quota failures are not misreported as bad credentials" {
    export MOCK_PROBE_FAIL=codex MOCK_PROBE_ERROR='Model is not supported for this account: fixture-secret-do-not-print'
    run_review; assert_failure
    [[ $output == *PR_REVIEW_CODEX_PROBE_MODEL* && $output != *'codex login'* && $output != *fixture-secret-do-not-print* ]]
    manifest_is '.phases.codex_auth_probe.failure_reason=="model_access"'
    export MOCK_PROBE_ERROR='Rate limit exceeded: fixture-secret-do-not-print'
    run_review --resume "$(run_directory)"; assert_failure
    [[ $output == *'usage or rate limit'* && $output != *'codex login'* && $output != *fixture-secret-do-not-print* ]]
    manifest_is '.phases.codex_auth_probe.failure_reason=="quota"'
    [ ! -e "$FIXTURE/stages" ]
}

@test "probe requires the diagnostic response and is bounded by its own timeout" {
    export MOCK_PROBE_REPLY='unexpected response'
    run_review; assert_failure
    manifest_is '.phases.claude_auth_probe.failure_reason=="unverified"'
    unset MOCK_PROBE_REPLY
    export MOCK_PROBE_SLEEP=claude PR_REVIEW_AUTH_TIMEOUT=1 PR_REVIEW_MODEL_TIMEOUT=30
    run_review --resume "$(run_directory)"; assert_failure
    [[ $output == *'Live probe timed out'* ]]
    manifest_is '.phases.claude_auth_probe.exit_status==124 and .phases.claude_auth_probe.failure_reason=="timeout"'
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
    [ ! -e "$FIXTURE/stages" ]
}

@test "resume rechecks live access even when reviews were completed" {
    run_review; assert_success
    export MOCK_PROBE_FAIL=codex MOCK_PROBE_EXIT=0
    run_review --resume "$(run_directory)"; assert_failure
    [ "$(cat "$FIXTURE/probes")" = $'claude\ncodex\nclaude\ncodex' ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
    manifest_is '.phases.codex_auth_probe.status=="failed" and .phases.synthesis.status=="completed"'
}

@test "explicit probe model override leaves the review model unchanged" {
    export PR_REVIEW_CODEX_PROBE_MODEL=account-cheap-model
    run_review; assert_success
    manifest_is '.phases.codex_auth_probe.model=="account-cheap-model" and .models.codex=="gpt-6.1-sol"'
    jq -se 'any(.[]; .tool=="codex" and (.args | index("account-cheap-model")))' "$FIXTURE/calls.jsonl"
}

@test "interrupting a live probe records interruption without requiring a Git snapshot" {
    export MOCK_PROBE_SLEEP=claude
    run python3 "$PROJECT_ROOT/tests/helpers/interrupt_runner.py" "$CLI" 7 --no-publish
    [ "$status" = 130 ]
    manifest_is '.phases.claude_auth_probe.status=="interrupted" and .phases.claude_auth_probe.exit_status==130'
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
    unset MOCK_PROBE_SLEEP
    run_review --resume "$(run_directory)"; assert_success
}
