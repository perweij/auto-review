#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "Ctrl-C escalates and returns when a model ignores both INT and TERM" {
    export MOCK_SLEEP_STAGE=codex MOCK_SLEEP_SECONDS=30 MOCK_IGNORE_SIGNALS=1
    run python3 "$PROJECT_ROOT/tests/helpers/interrupt_runner.py" "$CLI" 7 --no-publish
    [ "$status" = 130 ]
    [[ $output == *'Stopping remaining model processes with KILL'* ]]
    manifest_is '.phases.codex.status=="interrupted" and .phases.codex.exit_status==130'
    [ -s "$(run_directory)/reviews/claude-independent.md" ]
    unset MOCK_SLEEP_STAGE MOCK_SLEEP_SECONDS MOCK_IGNORE_SIGNALS
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\ncodex\nsynthesis\ncontributions' ]
}

@test "Codex repository error stops a hung process and retains the independent review" {
    export MOCK_CODEX_EVENT=git MOCK_CODEX_EVENT_HANG=1
    run timeout --kill-after=2s 22s "$CLI" 7 --no-publish
    [ "$status" = 1 ]
    [[ $output == *'Codex rejected the working directory as a Git repository'* ]]
    manifest_is '.phases.codex.status=="failed" and .phases.codex.failure_reason=="git_repository_unavailable" and .phases.codex.exit_status!=0'
    [ -s "$(run_directory)/reviews/claude-independent.md" ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex' ]
    unset MOCK_CODEX_EVENT MOCK_CODEX_EVENT_HANG
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\ncodex\nsynthesis\ncontributions' ]
}

@test "terminal Codex event fails even if the CLI exits zero with a report" {
    export MOCK_CODEX_EVENT=failed
    run_review; assert_failure
    manifest_is '.phases.codex.failure_reason=="codex_turn_failed" and .phases.codex.process_exit_status==0 and .phases.codex.exit_status==1'
    [ ! -e "$(run_directory)/reviews/codex.md" ]
    [ ! -e "$(run_directory)/reviews/synthesis.json" ]
    [[ $output != *fixture-provider-error-do-not-replay* ]]
}

@test "retryable Codex events do not prevent eventual success and stderr counts as activity" {
    export MOCK_CODEX_EVENT=retry MOCK_STDERR_STAGE=codex
    run_review; assert_success
    [[ $output == *'reported an error event and may be retrying'* ]]
    [[ $output == *'Process running:'* ]]
    [[ $output =~ [0-2]s\ since\ last\ output\ \(stdout/stderr\) ]]
    [[ $output != *'Working:'* && $output != *fixture-provider-error-do-not-replay* && $output != *fixture-stderr-do-not-replay* ]]
    manifest_is '.phases.codex.status=="completed"'
}



@test "structured Claude phases stream activity and retain reusable buffered-output cache identity" {
    export MOCK_STREAM_STAGE=synthesis
    run_review --processing-provider claude; assert_success
    [[ $output == *'[synthesis] Process running:'* ]]
    [[ $output =~ [0-2]s\ since\ last\ output\ \(stdout/stderr\) ]]
    [[ $output != *fixture-model-text-do-not-replay* ]]
    saved=$(run_directory)
    jq -se 'any(.[]; .type=="stream_event") and (last | .type=="result" and .is_error==false)' "$saved/logs/synthesis.raw.json"
    jq -e '[.[] | select(.stage=="synthesis" or .stage=="contributions") | .argv] |
        length==2 and all(.[]; .[index("--output-format")+1]=="stream-json" and
          index("--verbose")!=null and index("--include-partial-messages")!=null)' "$saved/logs/commands.json"
    # The saved command matches earlier buffered-JSON runs byte for byte.
    manifest_is '[.phases.synthesis.inputs.command,.phases.contributions.inputs.command] |
        all(.[]; .[index("--output-format")+1]=="json" and index("--verbose")==null and index("--include-partial-messages")==null)'
    run_review; assert_success
    [[ $output == *'Reusing completed synthesis'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}



@test "Git config environment overrides are removed as a complete group" {
    export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.ignorecase GIT_CONFIG_VALUE_0=true
    export GIT_CONFIG_PARAMETERS="'core.ignorecase'='true'"
    run_review; assert_success
    for stage in claude codex; do
        run grep -E '^GIT_CONFIG_(COUNT|KEY_|VALUE_|PARAMETERS)' "$FIXTURE/environment-$stage"
        [ "$status" = 1 ]
    done
    manifest_is '.removed_environment.codex | index("GIT_CONFIG_COUNT")!=null and index("GIT_CONFIG_KEY_0")!=null and index("GIT_CONFIG_VALUE_0")!=null and index("GIT_CONFIG_PARAMETERS")!=null'
    [ -s "$(run_directory)/logs/codex.git-preflight.log" ]
}
