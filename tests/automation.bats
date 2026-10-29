#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

auto_review() {
    run --separate-stderr "$CLI" 7 --auto-publish --json "$@"
}

anchored_findings() {
    jq -n '[{id:"F1",title:"Arithmetic",validity:"CONFIRMED",evidence:"Inspected source.txt",blocking:false,
      body_md:"Check the arithmetic.",primary_location:{path:"source.txt",side:"RIGHT",start_line:2,line:2},locations:[]}]' > "$FIXTURE/findings.json"
}

@test "economical automatic review publishes COMMENT once and repeats as a no-op" {
    anchored_findings
    auto_review --economical; assert_success
    manifest_is '.configuration.claude_model=="haiku" and .configuration.codex_model=="gpt-6.1-terra" and .configuration.processing_provider=="codex"'
    jq -e '.outcome=="published" and .exit_code==0 and (.receipts|length)>0 and .session and .log_path' <<< "$output"
    jq -e '.event=="COMMENT" and (.comments|length)==1' "$FIXTURE/api-payload.json"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    auto_review; assert_success
    jq -e '.outcome=="noop"' <<< "$output"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "automatic missing placement blocks without staging and leaves GUI-readable outcome" {
    auto_review
    [ "$status" = 3 ]
    jq -e '.outcome=="needs_attention" and .affected[0].id=="p1"' <<< "$output"
    draft_is '.comments[0].status=="unstaged"'
    [ ! -e "$FIXTURE/post-count" ]
    saved=$(session_directory)
    state=$(printf '{"protocol":1,"command":"read","session":"%s"}\n' "$saved" | "$CLI" api)
    jq -e '.result.automation.outcome=="needs_attention" and .result.analysis.displayed.status=="complete"' <<< "$state"
}

@test "automatic uncertain finding blocks an otherwise eligible batch" {
    discussion_fixture UNCERTAIN
    add_new_finding
    jq 'map(.primary_location={path:"source.txt",side:"RIGHT",start_line:2,line:2}|.locations=[])' "$FIXTURE/findings.json" > "$FIXTURE/findings.tmp"
    mv "$FIXTURE/findings.tmp" "$FIXTURE/findings.json"
    auto_review
    [ "$status" = 3 ]
    draft_is 'all(.comments[]; .status=="unstaged")'
    [ ! -e "$FIXTURE/post-count" ]
    [ ! -e "$FIXTURE/reply-count" ]
}

@test "automatic empty analysis exits without a post" {
    echo '[]' > "$FIXTURE/findings.json"
    auto_review; assert_success
    jq -e '.outcome=="noop"' <<< "$output"
    [ ! -e "$FIXTURE/post-count" ]
}

@test "automatic paused analysis preserves reports without an early draft ledger" {
    export MOCK_MATCH_CONFLICT=always
    auto_review
    [ "$status" = 3 ]
    manifest_is '.status=="paused"'
    [ -s "$(run_directory)/reviews/synthesis.json" ]
    [ ! -e "$(run_directory)/publication/drafts.json" ]
    unset MOCK_MATCH_CONFLICT
    run_review; assert_success
    draft_is '(.comments|length)==1'
}

@test "automatic first-run model failure retains session and operation log" {
    export MOCK_FAIL_STAGE=synthesis
    auto_review
    [ "$status" = 1 ]
    jq -e '.outcome=="failed" and .session and .generation and .log_path' <<< "$output"
    [ -s "$(jq -r '.log_path' <<< "$output")" ]
    [ ! -e "$(run_directory)/publication/drafts.json" ]
}

@test "automatic lost response is recovered without reposting" {
    anchored_findings
    export MOCK_POST=ambiguous
    auto_review; assert_success
    jq -e '.outcome=="published"' <<< "$output"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "automatic unsent publication remains a GUI handoff on repeated invocation" {
    anchored_findings
    export MOCK_POST=absent
    auto_review
    [ "$status" = 3 ]
    unset MOCK_POST
    auto_review
    [ "$status" = 3 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "automatic partial batch stops and retains the draft remainder" {
    discussion_fixture ADDITION
    add_new_finding
    jq 'map(.primary_location={path:"source.txt",side:"RIGHT",start_line:2,line:2}|.locations=[])' "$FIXTURE/findings.json" > "$FIXTURE/findings.tmp"
    mv "$FIXTURE/findings.tmp" "$FIXTURE/findings.json"
    export MOCK_MOVE_HEAD_AFTER_REPLY=1
    auto_review
    [ "$status" = 4 ]
    jq -e '.outcome=="partial"' <<< "$output"
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -e "$FIXTURE/post-count" ]
    auto_review
    [ "$status" = 4 ]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
}

@test "automatic mode never prompts even on a terminal" {
    anchored_findings
    tty_review '' --auto-publish
    assert_success
    [[ $output != *'Type yes:'* && $output != *'Choose action'* ]]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "automatic options reject incompatible modes before model work" {
    run "$CLI" 7 --auto-publish --no-publish
    [ "$status" = 2 ]
    run "$CLI" 7 --auto-publish --followup-thread thread:T1
    [ "$status" = 2 ]
    run "$CLI" 7 --json
    [ "$status" = 2 ]
    [ ! -e "$FIXTURE/stages" ]
}

@test "automatic model catalog rejection happens before analysis" {
    auto_review --codex-model unavailable
    [ "$status" = 1 ]
    jq -e '.error_code=="invalid_request"' <<< "$output"
    [ ! -e "$FIXTURE/stages" ]
}

@test "automatic cancellation returns JSON and preserves resumable analysis" {
    export MOCK_SLEEP_STAGE=codex MOCK_SLEEP_SECONDS=20
    run python3 "$PROJECT_ROOT/tests/helpers/interrupt_runner.py" "$CLI" 7 --auto-publish --json
    [ "$status" = 130 ]
    result=$(tail -n 1 "$FIXTURE/interrupt-output")
    jq -e '.outcome=="interrupted" and .exit_code==130 and .session' <<< "$result"
    manifest_is '.status=="failed" and .phases.claude.status=="completed"'
}

@test "automatic trust failure is saved and recoverable with an exact snapshot token" {
    anchored_findings
    echo 'Changed agent instructions' > AGENTS.md
    git add AGENTS.md
    git commit -qm config
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    auto_review
    [ "$status" = 3 ]
    jq -e '.error_code=="confirmation_required" and .trusted_snapshot and .agent_config_paths==["AGENTS.md"]' <<< "$output"
    token=$(jq -r '.trusted_snapshot' <<< "$output")
    [ ! -e "$FIXTURE/stages" ]
    auto_review --trust-agent-config "$token"; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "automatic mode preserves manually edited drafts before any new analysis" {
    run_review; assert_success
    saved=$(session_directory)
    state=$(printf '{"protocol":1,"command":"read","session":"%s"}\n' "$saved" | "$CLI" api)
    request=$(jq -c '.result | {protocol:1,command:"mutate",session,generation,revision,operation:"edit",comment:"p1",body:"Exact manual wording"}' <<< "$state")
    printf '%s\n' "$request" | "$CLI" api >/dev/null
    count=$(wc -l < "$FIXTURE/stages")
    auto_review
    [ "$status" = 3 ]
    [ "$(wc -l < "$FIXTURE/stages")" = "$count" ]
    draft_is '.comments[0].body=="Exact manual wording"'
    [ ! -e "$FIXTURE/post-count" ]
}
