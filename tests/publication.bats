#!/usr/bin/env bats
# Each Bats test is intentionally isolated in its own subshell.
# shellcheck disable=SC2030,SC2031
load helpers/fixture

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
