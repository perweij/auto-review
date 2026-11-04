#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "usage includes live probes and roles and survives generation resumes" {
    run_review
    assert_success
    [[ $output == *'Provider / meter'* ]]
    [[ $output == *'Model'*'Effort'*'Reported tokens'* ]]
    local session ledger before
    session=$(session_directory)
    ledger="$session/usage/ledger.json"
    jq -e '.version == 1 and (.calls | length) == 6 and
        ([.calls[].role] | sort) == (["auth-probe/claude","auth-probe/codex","reviewer-1","reviewer-2","synthesis","contributions"] | sort)' "$ledger"
    jq -e 'all(.calls[]; .model != null and .effort != null)' "$ledger"
    before=$(jq '.calls | length' "$ledger")
    run_review
    assert_success
    # Existing auth probes run again; all four completed substantive stages are reused.
    [ "$(jq '.calls | length' "$ledger")" = "$((before + 2))" ]
    [ "$(jq '[.calls[] | select(.role == "reviewer-1")] | length' "$ledger")" = 1 ]
    run bash -c 'printf "%s\n" "$1" | "$2" api' _ "$(jq -nc --arg session "$session" '{protocol:1,command:"read",session:$session}')" "$CLI"
    assert_success
    printf '%s\n' "$output" | jq -e 'select(.type == "result") | .result.usage.calls == 8'
}

@test "provider failure retains usage and prints cumulative tables" {
    export MOCK_FAIL_STAGE=synthesis
    run_review
    assert_failure
    [[ $output == *'Provider / meter'* ]]
    local ledger
    ledger="$(session_directory)/usage/ledger.json"
    jq -e 'any(.calls[]; .role == "synthesis" and .status == "failed")' "$ledger"
    jq -e 'all(.calls[]; .status != "running")' "$ledger"
}
