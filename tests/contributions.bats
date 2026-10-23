#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "matching receives complete conversations including later replies" {
    discussion_fixture
    run_review; assert_success
    local snapshot
    snapshot=$(jq -r .discussion.path "$(run_directory)/manifest.json")
    jq -e '.items[]|select(.key=="thread:T1")|(.comments|length)==2 and (.comments[1].body|contains("reuses the same key"))' "$snapshot"
    grep -q "$snapshot" "$FIXTURE/contributions-stdin"
    jq -e '.proposals[0] | .selected and .destination.root_id==101 and (.body_md|contains("timeout case discussed above"))' "$(run_directory)/publication/contributions.json"
}

@test "duplicates are suppressed but remain blockers for formal approval" {
    discussion_fixture DUPLICATE
    tty_review 'c|a|no|n'; assert_success
    [[ $output == *'Nothing new is selected'* && $output == *'Blocking findings remain'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    [ ! -f "$FIXTURE/reply-count" ]
    jq -e '.blocking_findings==1' "$(run_directory)/reviews/synthesis.json"
    jq -e '.proposals[0]|.classification=="DUPLICATE" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "useful addition publishes a contextual reply without a noisy summary" {
    discussion_fixture
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
    jq -e '.in_reply_to_id==101 and (.body|contains("timeout case discussed above")) and (.body|contains("action=a1"))' "$FIXTURE/reply-response.json"
    manifest_is '.publication.status=="published" and .publication.published_actions==1'
}

@test "new issues follow thread replies in one confirmed batch" {
    discussion_fixture
    add_new_finding
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    jq -e '.[0].kind=="reply" and .[1].kind=="review" and all(.[];.status=="published")' "$(run_directory)/publication/actions.json"
    jq -e '(.body|contains("Multiplication became addition")) and (.body|contains("timeout case")|not)' "$FIXTURE/published.json"
}

@test "multiple additions to a thread are consolidated into one reply" {
    discussion_fixture
    add_new_finding
    jq '.contributions[1] |= (.classification="ADDITION"|.discussion_keys=["thread:T1"]|.target_key="thread:T1")' "$FIXTURE/contribution-model.json" > "$FIXTURE/changed.json"
    mv "$FIXTURE/changed.json" "$FIXTURE/contribution-model.json"
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    jq -e '(.body|contains("timeout case")) and (.body|contains("Multiplication"))' "$FIXTURE/reply-response.json"
}

@test "resolved follow-up is opt-in and never reopens a thread" {
    discussion_fixture REVISIT
    tty_review 'c|n'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    tty_review 't p1|c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    run ! grep -E 'unresolveReviewThread|resolveReviewThread' "$FIXTURE/calls.jsonl"
}

@test "resolved metadata enforces opt-in even if matcher calls it an addition" {
    discussion_fixture ADDITION
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    run_review; assert_success
    jq -e '.proposals[0]|.classification=="REVISIT" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "outdated is not resolved, but an explicitly concluded discussion is opt-in" {
    discussion_fixture
    jq '.[0].isOutdated=true' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    run_review; assert_success
    jq -e '.proposals[0].selected' "$(run_directory)/publication/contributions.json"
    # Force a new matching input through an actual discussion change.
    jq '.[1].body="Agreed; closing this point."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    jq '.contributions[0].classification="REVISIT"' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    run_review --resume "$(run_directory)"; assert_success
    jq -e '.proposals[0]|.classification=="REVISIT" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "timeline follow-up stays in main review with a source link" {
    discussion_fixture
    jq '.contributions[0] |= (.discussion_keys=["comment:10"]|.target_key=null)' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    tty_review 'c|yes'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    jq -e '.body|contains("#issuecomment-10")' "$FIXTURE/published.json"
}

@test "unknown model destinations are rejected before publication" {
    discussion_fixture
    jq '.contributions[0] |= (.discussion_keys=["thread:invented"]|.target_key="thread:invented")' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    run_review; assert_failure
    [[ $output == *'Invalid contribution schema'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "all thread pages and all REST replies are retained" {
    discussion_fixture
    jq '. + [.[0] | .id="T2" | .comments.nodes[0].databaseId=201 | .comments.totalCount=1]' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    jq '. + [.[0] | .id=201]' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    export MOCK_THREAD_PAGES=1
    run_review; assert_success
    jq -e '[.items[]|select(.kind=="thread")]|length==2' "$(jq -r .discussion.path "$(run_directory)/manifest.json")"
}

@test "GraphQL partial response and unavailable inline discussion pause publication" {
    export MOCK_GRAPHQL_FAIL=1
    tty_review 'c|yes'; assert_success
    [[ $output == *'Publication paused'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    manifest_is '.discussion.complete==false and .publication.status=="paused"'
    unset MOCK_GRAPHQL_FAIL
    export MOCK_INLINE_FAIL=1
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ ! -f "$FIXTURE/post-count" ]
}

@test "unavailable reply permission disables that proposal" {
    discussion_fixture
    jq '.[0].viewerCanReply=false' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    tty_review 't p1|c|n'; assert_success
    [[ $output == *'reply unavailable'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "unchanged proposals preserve edits and selections across discussion refresh" {
    discussion_fixture REVISIT
    printf '#!/usr/bin/env bash\nprintf "Human contextual reply\\n" > "$1"\n' > "$FIXTURE/bin/editor"; chmod +x "$FIXTURE/bin/editor"
    export EDITOR="$FIXTURE/bin/editor"
    tty_review 't p1|e p1|n'; assert_success
    jq '.[1].body += " Additional context."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    run_review --resume "$(run_directory)"; assert_success
    jq -e '.proposals[0]|.selected and (.body_md|contains("Human contextual reply"))' "$(run_directory)/publication/contributions.json"
    [ "$(grep -c '^contributions$' "$FIXTURE/stages")" = 2 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
}

@test "changed proposal archives edit and resets resolved selection" {
    discussion_fixture REVISIT
    tty_review 't p1|n'; assert_success
    jq '.[1].body += " Changed conclusion."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    jq '.contributions[0].body_md="Revised proposal responding to the new conclusion."' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    run_review --resume "$(run_directory)"; assert_success
    jq -e '.proposals[0]|(.selected|not) and (.body_md|startswith("Revised"))' "$(run_directory)/publication/contributions.json"
    jq -e '.proposals[0].selected' "$(run_directory)/publication/contributions.previous.json"
}

@test "discussion change after confirmation requires refreshed proposal confirmation" {
    discussion_fixture
    jq '.[1].body += " A colleague already covered restarts too."' "$FIXTURE/inline.json" > "$FIXTURE/inline-next.json"
    export MOCK_CHANGE_AT=2 MOCK_CHANGE_MATCH_TO_DUPLICATE=1
    tty_review 'c|yes|n'; assert_success
    [[ $output == *'Discussion changed'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
    [ "$(grep -c '^contributions$' "$FIXTURE/stages")" = 2 ]
}

@test "validity conflict reruns synthesis but preserves both independent reviews" {
    export MOCK_MATCH_CONFLICT=once
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\ncontributions' ]
    jq -e '.findings[0].validity=="REJECTED" and .blocking_findings==0' "$(run_directory)/reviews/synthesis.json"
}

@test "unresolved validity conflict pauses publication" {
    export MOCK_MATCH_CONFLICT=always
    tty_review 'c|yes'; assert_success
    [[ $output == *'still challenges finding validity'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    manifest_is '.publication.status=="paused"'
}

@test "ambiguous reply is recovered and resume never duplicates it" {
    discussion_fixture
    export MOCK_REPLY_AMBIGUOUS=1
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    jq '.[0].status="submitting"|del(.[0].result)' "$(run_directory)/publication/actions.json" > "$FIXTURE/new.json"
    cp "$FIXTURE/new.json" "$(run_directory)/publication/actions.json"
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
}

@test "partial batch preserves posted reply and resumes only failed review" {
    discussion_fixture
    add_new_finding
    export MOCK_POST=reject
    tty_review 'c|yes|no'; assert_failure
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [[ $output == *'Partially published'* ]]
    unset MOCK_POST
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    jq -e 'all(.[];.status=="published")' "$(run_directory)/publication/actions.json"
}

@test "legacy run cannot guess new publication state" {
    run_review; assert_success
    jq '.format_version=1' "$(run_directory)/manifest.json" > "$FIXTURE/new.json"; cp "$FIXTURE/new.json" "$(run_directory)/manifest.json"
    run_review --resume "$(run_directory)"; assert_failure
    [[ $output == *'Legacy run remains readable'* ]]
}

@test "missing replies detected by thread count pause publication" {
    discussion_fixture
    jq '.[0].comments.totalCount=3' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    tty_review 'c|yes'; assert_success
    manifest_is '.discussion.complete==false and .publication.status=="paused"'
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "publication interruption after a reply resumes the remaining review" {
    discussion_fixture
    add_new_finding
    export MOCK_INTERRUPT_AFTER_REPLY=1
    tty_review 'c|yes'
    [ "$status" = 130 ]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    unset MOCK_INTERRUPT_AFTER_REPLY
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "head update between batch actions preserves reply and blocks the review" {
    discussion_fixture
    add_new_finding
    export MOCK_MOVE_HEAD_AFTER_REPLY=1
    tty_review 'c|yes'; assert_failure
    [[ $output == *'PR head changed'* && $output == *'Partially published'* ]]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "uncertain proposal starts unselected and is available for explicit selection" {
    discussion_fixture UNCERTAIN
    tty_review 'c|t p1|p|yes'; assert_success
    [[ $output == *'Nothing new is selected'* ]]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
}

@test "noninteractive mode never posts selected thread replies" {
    discussion_fixture
    run "$CLI" 7; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "malformed duplicate lookup cannot falsely mark a post as published" {
    export MOCK_POST=ambiguous MOCK_BAD_LOOKUP=1
    tty_review 'c|yes'; assert_failure
    [[ $output == *'Malformed publication lookup'* ]]
    jq -e '.[0].status=="submitting" and .[0].result==null' "$(run_directory)/publication/actions.json"
    unset MOCK_BAD_LOOKUP
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "thread resolution during confirmation resets selection before posting" {
    discussion_fixture
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads-next.json"
    export MOCK_CHANGE_AT=3
    tty_review 'c|yes|n'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    jq -e '.proposals[0]|.classification=="REVISIT" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "discussion change between batch actions stops remaining posts with partial status" {
    discussion_fixture
    add_new_finding
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads-next.json"
    export MOCK_CHANGE_AT=4
    tty_review 'c|yes|n'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
    [[ $output == *'Partially published'* && $output == *'Posted:'* ]]
}

@test "inspection shows the draft and complete thread without implementation fields" {
    discussion_fixture
    tty_review 'i p1|n'; assert_success
    [[ $output == *'Full matched discussion:'* && $output == *'@author'* && $output == *'Does that address the concern?'* ]]
    [[ $output != *'"target_key"'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "retry under a different account uses its own marker author for deduplication" {
    export MOCK_POST=reject
    tty_review 'c|yes|no'; assert_failure
    export MOCK_POST=ambiguous MOCK_VIEWER=second-reviewer
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 2 ]
    jq -e '.[0].viewer=="reviewer" and .[1].viewer=="second-reviewer" and .[1].status=="published"' "$(run_directory)/publication/actions.json"
}
