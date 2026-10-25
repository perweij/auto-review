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
    jq -e '.proposals[0] | (.selected|not) and .destination.root_id==101 and (.body_md|contains("timeout case discussed above"))' "$(run_directory)/publication/contributions.json"
}

@test "duplicates are suppressed but remain blockers for formal approval" {
    discussion_fixture DUPLICATE
    tty_review 'p|v APPROVE|p|no|q'; assert_success
    [[ $output == *'Nothing is staged'* && $output == *'retained blocking findings remain'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    [ ! -f "$FIXTURE/reply-count" ]
    jq -e '.blocking_findings==1' "$(run_directory)/reviews/synthesis.json"
    jq -e '.proposals[0]|.classification=="DUPLICATE" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "useful addition publishes a contextual reply without a noisy summary" {
    discussion_fixture
    tty_review 's p1|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
    jq -e '.in_reply_to_id==101 and (.body|contains("timeout case discussed above")) and (.body|contains("action=api-"))' "$FIXTURE/reply-response.json"
    manifest_is '.publication.status=="published" and .publication.published_actions==1'
}

@test "new issues follow thread replies in one confirmed batch" {
    discussion_fixture
    add_new_finding
    tty_review 's p1|a p2|overall|s p2|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    draft_is '.batches[0].actions | .[0].kind=="reply" and .[1].kind=="review" and all(.[];.status=="published")'
    jq -e '(.body|contains("Multiplication became addition")) and (.body|contains("timeout case")|not)' "$FIXTURE/api-payload.json"
}

@test "separately edited additions remain exact separate replies" {
    discussion_fixture
    add_new_finding
    jq '.contributions[1] |= (.classification="ADDITION"|.discussion_keys=["thread:T1"]|.target_key="thread:T1")' "$FIXTURE/contribution-model.json" > "$FIXTURE/changed.json"
    mv "$FIXTURE/changed.json" "$FIXTURE/contribution-model.json"
    tty_review 's p1|s p2|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 2 ]
    jq -se 'length==2 and (.[0].body|contains("timeout case")) and (.[1].body|contains("Multiplication"))' "$FIXTURE/published-replies.jsonl"
}

@test "resolved follow-up is opt-in and never reopens a thread" {
    discussion_fixture REVISIT
    tty_review 'p|q'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    tty_review 's p1|p|yes|q' --resume "$(run_directory)"; assert_success
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
    jq -e '.proposals[0].selected==false' "$(run_directory)/publication/contributions.json"
    # Force a new matching input through an actual discussion change.
    jq '.[1].body="Agreed; closing this point."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    jq '.contributions[0].classification="REVISIT"' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    run_review --resume "$(run_directory)"; assert_success
    jq -e '.proposals[0]|.classification=="REVISIT" and (.selected|not)' "$(run_directory)/publication/contributions.json"
}

@test "timeline follow-up stays in main review with a source link" {
    discussion_fixture
    jq '.contributions[0] |= (.discussion_keys=["comment:10"]|.target_key=null)' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    draft_is '.comments[0].discussion_keys==["comment:10"]'
    jq -e '.body|startswith("Reusing the key addresses")' "$FIXTURE/api-payload.json"
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
    tty_review 's p1|p|yes|q'; assert_success
    [[ $output == *'Publication paused'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    manifest_is '.discussion.complete==false and .publication.status=="paused"'
    unset MOCK_GRAPHQL_FAIL
    export MOCK_INLINE_FAIL=1
    tty_review 's p1|p|yes|q' --resume "$(run_directory)"; assert_success
    [ ! -f "$FIXTURE/post-count" ]
}

@test "unavailable reply permission disables that proposal" {
    discussion_fixture
    jq '.[0].viewerCanReply=false' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
    tty_review 's p1|p|q'; assert_success
    [[ $output == *'no longer accepts replies'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "rematching preserves human edits in history and starts replacement drafts unstaged" {
    discussion_fixture REVISIT
    run_review; assert_success
    previous=$(run_directory)
    printf '#!/usr/bin/env bash\nprintf "Human contextual reply\\n" > "$1"\n' > "$FIXTURE/bin/editor"; chmod +x "$FIXTURE/bin/editor"
    export EDITOR="$FIXTURE/bin/editor"
    tty_drafts 'e p1|s p1|q'; assert_success
    jq '.[1].body += " Additional context."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    run_review; assert_success
    jq -e '.comments[0].body=="Human contextual reply\n" and .comments[0].status=="staged"' "$previous/publication/drafts.json"
    draft_is '.comments[0].status=="unstaged" and .comments[0].body!="Human contextual reply\n"'
    [ "$(grep -c '^contributions$' "$FIXTURE/stages")" = 2 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
}

@test "changed proposal archives edit and resets resolved selection" {
    discussion_fixture REVISIT
    tty_review 's p1|q'; assert_success
    previous=$(run_directory)
    jq '.[1].body += " Changed conclusion."' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
    jq '.contributions[0].body_md="Revised proposal responding to the new conclusion."' "$FIXTURE/contribution-model.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/contribution-model.json"
    run_review --resume "$(run_directory)"; assert_success
    jq -e '.proposals[0]|(.selected|not) and (.body_md|startswith("Revised"))' "$(run_directory)/publication/contributions.json"
    jq -e '.comments[0].status=="staged"' "$previous/publication/drafts.json"
}

@test "discussion change after confirmation requires refreshed proposal confirmation" {
    discussion_fixture
    jq '.[1].body += " A colleague already covered restarts too."' "$FIXTURE/inline.json" > "$FIXTURE/inline-next.json"
    export MOCK_CHANGE_AT=2 MOCK_CHANGE_MATCH_TO_DUPLICATE=1
    tty_review 's p1|p|yes|q'; assert_success
    [[ $output == *'Discussion changed'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
    [ "$(grep -c '^contributions$' "$FIXTURE/stages")" = 1 ]
}

@test "validity conflict reruns synthesis but preserves both independent reviews" {
    export MOCK_MATCH_CONFLICT=once
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\ncontributions' ]
    jq -e '.findings[0].validity=="REJECTED" and .blocking_findings==0' "$(run_directory)/reviews/synthesis.json"
}

@test "unresolved validity conflict pauses publication" {
    export MOCK_MATCH_CONFLICT=always
    tty_review 's p1|p|yes|q'; assert_success
    [[ $output == *'still challenges finding validity'* ]]
    [ ! -f "$FIXTURE/post-count" ]
    manifest_is '.publication.status=="paused"'
}

@test "ambiguous reply is recovered and resume never duplicates it" {
    discussion_fixture
    export MOCK_REPLY_AMBIGUOUS=1
    tty_review 's p1|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    draft_is '.batches[0].actions[0].status=="ambiguous"'
    tty_review 'resolve|p|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    draft_is '.comments[0].status=="published"'
}

@test "partial batch preserves posted reply and resumes only failed review" {
    discussion_fixture
    add_new_finding
    export MOCK_POST=reject
    tty_review 's p1|a p2|overall|s p2|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    draft_is '.comments[0].status=="published" and .batches[0].actions[1].status=="ambiguous"'
    unset MOCK_POST
    tty_drafts 'resolve|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    draft_is 'all(.comments[];.status=="published")'
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
    tty_review 's p1|p|yes|q'; assert_success
    manifest_is '.discussion.complete==false and .publication.status=="paused"'
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "publication interruption after a reply resumes the remaining review" {
    discussion_fixture
    add_new_finding
    export MOCK_INTERRUPT_AFTER_REPLY=1
    tty_review 's p1|a p2|overall|s p2|p|yes|q'
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    unset MOCK_INTERRUPT_AFTER_REPLY
    tty_drafts 'resolve|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "head update between batch actions preserves reply and blocks the review" {
    discussion_fixture
    add_new_finding
    export MOCK_MOVE_HEAD_AFTER_REPLY=1
    tty_review 's p1|a p2|overall|s p2|p|yes|q'; assert_success
    [[ $output == *'PR head changed'* ]]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
    draft_is '.comments[0].status=="published"'
}

@test "uncertain proposal starts unselected and is available for explicit selection" {
    discussion_fixture UNCERTAIN
    tty_review 'p|s p1|p|yes|q'; assert_success
    [[ $output == *'Nothing is staged'* ]]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
}

@test "noninteractive mode never posts selected thread replies" {
    discussion_fixture
    run "$CLI" 7; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "malformed duplicate lookup cannot falsely mark a post as published" {
    export MOCK_POST=ambiguous
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    export MOCK_BAD_LOOKUP=1
    tty_drafts 'resolve|q'; assert_success
    [[ $output == *'Malformed publication lookup'* ]]
    draft_is '.batches[0].actions[0].status=="ambiguous"'
    unset MOCK_BAD_LOOKUP
    tty_drafts 'resolve|q'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "thread resolution during confirmation resets selection before posting" {
    discussion_fixture
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads-next.json"
    export MOCK_CHANGE_AT=3
    tty_review 's p1|p|yes|q'; assert_success
    [ ! -f "$FIXTURE/reply-count" ]
    draft_is '.comments[0].status=="unstaged" and .comments[0].needs_attention!=null'
}

@test "discussion change between batch actions stops remaining posts with partial status" {
    discussion_fixture
    add_new_finding
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads-next.json"
    export MOCK_CHANGE_AT=5
    tty_review 's p1|a p2|overall|s p2|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -f "$FIXTURE/post-count" ]
    draft_is '.comments[0].status=="published"'
}

@test "inspection shows the draft and complete thread without implementation fields" {
    discussion_fixture
    tty_review 'i p1|n'; assert_success
    [[ $output == *'@colleague'* && $output == *'@author'* && $output == *'Does that address the concern?'* ]]
    [[ $output != *'"target_key"'* ]]
    [ ! -f "$FIXTURE/reply-count" ]
}

@test "changed account blocks a retry and retains the original marker author" {
    export MOCK_POST=reject
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    export MOCK_VIEWER=second-reviewer
    unset MOCK_POST
    tty_drafts 'resolve|p|q'; assert_success
    [[ $output == *'GitHub account changed'* ]]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    draft_is '.batches[0].actions[0].viewer=="reviewer"'
}
