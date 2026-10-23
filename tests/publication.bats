#!/usr/bin/env bats
# Each Bats test is intentionally isolated in its own subshell.
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "replies-only with no replies returns to the menu and preview does not post" {
    tty_review 'p|v|t p1|v|n'; assert_success
    [[ $output == *'Publish selected items (automatic destinations)'* && $output == *'Replies-only shortcut'* ]]
    [[ $output != *'[p] Publish'* ]]
    [[ $output == *'No selected unpublished thread replies'* ]]
    [[ $output == *'review COMMENT'* && $output == *'No selected unpublished drafts to preview'* ]]
    [[ $output == *'Exit without publishing (default: Enter)'* && $output == *'Leaving the review menu'* ]]
    [[ $output != *'Publish this exact batch?'* ]]
    [ "$(printf '%s' "$output" | grep -o 'Choose action' | wc -l)" = 5 ]
    [ ! -e "$FIXTURE/post-count" ]
    [ ! -e "$FIXTURE/reply-count" ]
}

@test "preview shows selected thread replies without publishing or asking to publish" {
    discussion_fixture ADDITION
    tty_review 'v|n'; assert_success
    [[ $output == *'reply COMMENT'* && $output == *'#discussion_r101'* ]]
    [[ $output != *'Publish this exact batch?'* ]]
    jq -e 'length==1 and all(.[];.status=="draft")' "$(run_directory)/publication/actions.json"
    [ ! -e "$FIXTURE/post-count" ]
    [ ! -e "$FIXTURE/reply-count" ]
}

@test "comment review requires selection and final confirmation and pins commit" {
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    jq -e --arg head "$HEAD_SHA" '.state == "COMMENT" and .commit_id == $head and (.body | contains("<!-- pr-review run="))' "$FIXTURE/published.json"
    manifest_is '.publication.status == "published"'
}

@test "request changes publication" {
    tty_review 'r|yes'; assert_success
    jq -e '.state == "REQUEST_CHANGES"' "$FIXTURE/published.json"
}

@test "approval with blocking findings requires an extra confirmation" {
    export MOCK_BLOCKERS=2
    tty_review 'a|yes|yes'; assert_success
    [[ $output == *'Blocking findings remain'* ]]
    jq -e '.state == "APPROVE"' "$FIXTURE/published.json"
}

@test "default choice and declined final confirmation never publish" {
    tty_review ''; assert_success
    [ ! -f "$FIXTURE/post-count" ]
    tty_review 'c|no' --resume "$(run_directory)"; assert_success
    [ ! -f "$FIXTURE/post-count" ]
}

@test "GitHub rejects publication but draft and diagnostic survive" {
    export MOCK_POST=reject
    tty_review 'c|yes|no'; assert_failure
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    [ -s "$(run_directory)/publication/review-body.md" ]
    grep -q '403' "$(run_directory)/logs/publication.stderr.log"
    manifest_is '.publication.status == "failed"'
}

@test "PR head updated during models blocks publication" {
    export MOCK_MOVE_HEAD=1
    tty_review 'c|yes'; assert_failure
    [[ $output == *'PR head changed'* ]]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "ordinary base advance permits publication with informational note" {
    export MOCK_MOVE_BASE=1
    tty_review 'c|yes'; assert_success
    [[ $output == *'base has advanced 1 commits'* ]]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "base rewrite changing merge-base blocks publication" {
    export MOCK_REWRITE_BASE=1
    tty_review 'c|yes'; assert_failure
    [[ $output == *'Merge-base changed'* ]]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "edit menu submits exactly the human-edited body and marker" {
    printf '#!/usr/bin/env bash\nprintf "Human edited body\\n" > "$1"\n' > "$FIXTURE/bin/editor"
    chmod +x "$FIXTURE/bin/editor"
    export EDITOR="$FIXTURE/bin/editor"
    tty_review 'e|c|yes'; assert_success
    jq -e '.body | startswith("Human edited body\n\n\n<!-- pr-review run=")' "$FIXTURE/published.json"
}

@test "ambiguous success finds marker and does not post twice, including resume" {
    export MOCK_POST=ambiguous
    tty_review 'c|yes'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    # Emulate crash after GitHub accepted a review, before local state completed.
    jq '.publication.status="submitting"' "$(run_directory)/manifest.json" > "$FIXTURE/changed.json"
    cp "$FIXTURE/changed.json" "$(run_directory)/manifest.json"
    tty_review 'c|yes' --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    manifest_is '.publication.status == "published"'
}

@test "absent marker after ambiguous error offers explicit retry" {
    export MOCK_POST=absent
    tty_review 'c|yes|yes|c|yes'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 2 ]
    [[ $output == *'Retry publication of remaining actions?'* ]]
}

@test "self-authored PR only offers comment" {
    export MOCK_AUTHOR=reviewer
    tty_review 'a|c|yes'; assert_success
    [[ $output != *'[a] Publish'* ]]
    jq -e '.state == "COMMENT"' "$FIXTURE/published.json"
}

@test "closed PR skips publication menu" {
    export MOCK_PR_STATE=MERGED
    tty_review 'c|yes'; assert_success
    [[ $output == *'publication disabled'* && $output != *'Choose action'* ]]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "agent configuration needs affirmative trust confirmation even with no-publish" {
    echo 'trusted instructions' > AGENTS.md
    git add .; git commit -qm config
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    tty_review 'no' --no-publish; assert_failure
    [ ! -f "$FIXTURE/stages" ]
    tty_review 'yes' --no-publish --resume "$(run_directory)"; assert_success
    manifest_is '.agent_config_confirmed == true'
}

@test "selection toggles update the automatic destination summary" {
    discussion_fixture ADDITION
    add_new_finding
    tty_review 't p2|c|yes'; assert_success
    [[ $output == *'1 thread reply + 1 COMMENT review containing 1 item'* ]]
    [[ $output == *'Will post: 1 thread reply.'* ]]
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "uncertain resolved-thread match stays uncertain and requires selection" {
    discussion_fixture UNCERTAIN
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads.tmp"
    mv "$FIXTURE/threads.tmp" "$FIXTURE/threads.json"
    tty_review 'c|n'; assert_success
    [[ $output == *'[ ] p1 UNCERTAIN: Retry after process restart'* ]]
    [[ $output == *'Reply to resolved thread · source.txt:1'* ]]
    [[ $output == *'Uncertain match or evidence; inspect before selecting.'* ]]
    [[ $output == *'Selected contributions: none.'* ]]
    jq -e '.proposals[0] | .classification=="UNCERTAIN" and (.selected|not)' "$(run_directory)/publication/contributions.json"
    [ ! -e "$FIXTURE/reply-count" ]
    [ ! -e "$FIXTURE/post-count" ]
}
