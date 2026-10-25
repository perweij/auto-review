#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "fresh drafts are unstaged and inspection never posts" {
    tty_review 'p|i p1|q'; assert_success
    [[ $output == *'Nothing is staged'* && $output == *Unstaged* ]]
    draft_is '.comments[0].status=="unstaged" and .batches==[]'
    [ ! -e "$FIXTURE/post-count" ]
}

@test "preview of a staged reply can be declined without publication" {
    discussion_fixture ADDITION
    tty_review 's p1|p|no|q'; assert_success
    [[ $output == *'replies only'* && $output == *'timeout case discussed above'* ]]
    draft_is '.comments[0].status=="staged" and .batches==[] and .prepared!=null'
    [ ! -e "$FIXTURE/reply-count" ]
}

@test "comment review requires staging and confirmation and pins commit" {
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    jq -e --arg head "$HEAD_SHA" '.event=="COMMENT" and .commit_id==$head and (.body|contains("<!-- pr-review generation="))' "$FIXTURE/api-payload.json"
    manifest_is '.publication.status=="published"'
    draft_is '.comments[0].status=="published" and .batches[0].actions[0].status=="published"'
}

@test "request changes requires explicit verdict choice" {
    tty_review 'v REQUEST_CHANGES|p|yes|q'; assert_success
    jq -e '.event=="REQUEST_CHANGES"' "$FIXTURE/api-payload.json"
}

@test "approval preview discloses blocking findings" {
    export MOCK_BLOCKERS=2
    tty_review 'v APPROVE|p|yes|q'; assert_success
    [[ $output == *'retained blocking findings remain'* ]]
    jq -e '.event=="APPROVE"' "$FIXTURE/api-payload.json"
}

@test "default choice and declined confirmation never publish" {
    tty_review ''; assert_success
    tty_drafts 'a p1|overall|s p1|p|no|q'; assert_success
    [ ! -f "$FIXTURE/post-count" ]
}

@test "rejected publication retains exact draft and uncertain intent" {
    export MOCK_POST=reject
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    [[ $output == *'HTTP 403'* ]]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    draft_is '.comments[0].body!="" and .batches[0].actions[0].status=="ambiguous"'
}

@test "PR head updated during models blocks publication" {
    export MOCK_MOVE_HEAD=1
    tty_review 'a p1|overall|s p1|p|q'; assert_success
    [[ $output == *'PR head changed'* ]]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "ordinary base advance permits publication with informational note" {
    export MOCK_MOVE_BASE=1
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    [[ $output == *'base has advanced 1 commits'* ]]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "base rewrite changing merge-base blocks publication" {
    export MOCK_REWRITE_BASE=1
    tty_review 'a p1|overall|s p1|p|q'; assert_success
    [[ $output == *'Merge-base changed'* ]]
    [ ! -f "$FIXTURE/post-count" ]
}

@test "editor submits exact human text including trailing newlines" {
    printf '#!/usr/bin/env bash\nprintf "Human edited body\\n" > "$1"\n' > "$FIXTURE/bin/editor"
    chmod +x "$FIXTURE/bin/editor"
    export EDITOR="$FIXTURE/bin/editor"
    tty_review 'e p1|a p1|overall|s p1|p|yes|q'; assert_success
    jq -e '.body|startswith("Human edited body\n\n\n<!-- pr-review generation=")' "$FIXTURE/api-payload.json"
}

@test "lost successful response is recovered without a duplicate post" {
    export MOCK_POST=ambiguous
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    draft_is '.batches[0].actions[0].status=="ambiguous"'
    tty_review 'resolve|p|q'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    draft_is '.comments[0].status=="published"'
}

@test "confirmed absence after lost response permits an explicitly confirmed retry" {
    export MOCK_POST=absent
    tty_review 'a p1|overall|s p1|p|yes|resolve|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/post-count")" = 2 ]
    draft_is '.comments[0].status=="published"'
}

@test "self-authored PR rejects approval but allows comment" {
    export MOCK_AUTHOR=reviewer
    tty_review 'v APPROVE|a p1|overall|s p1|p|yes|q'; assert_success
    [[ $output == *'You are the PR author'* ]]
    jq -e '.event=="COMMENT"' "$FIXTURE/api-payload.json"
}

@test "closed PR remains readable but publication is blocked" {
    export MOCK_PR_STATE=MERGED
    tty_review 'i p1|a p1|overall|s p1|p|q'; assert_success
    [ ! -f "$FIXTURE/post-count" ]
}

@test "agent configuration requires affirmative snapshot trust even with no-publish" {
    echo 'trusted instructions' > AGENTS.md
    git add .; git commit -qm config
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    tty_review 'no' --no-publish; assert_failure
    [ ! -f "$FIXTURE/stages" ]
    tty_review 'yes' --no-publish --resume "$(run_directory)"; assert_success
    manifest_is '.agent_config_confirmed==true'
}

@test "staging only a reply leaves a new overall finding unpublished" {
    discussion_fixture ADDITION
    add_new_finding
    tty_review 's p1|p|yes|q'; assert_success
    [ "$(cat "$FIXTURE/reply-count")" = 1 ]
    [ ! -e "$FIXTURE/post-count" ]
    draft_is '.comments[0].status=="published" and .comments[1].status=="unstaged"'
}

@test "uncertain resolved-thread match stays unstaged until explicitly selected" {
    discussion_fixture UNCERTAIN
    jq '.[0].isResolved=true' "$FIXTURE/threads.json" > "$FIXTURE/threads.tmp"
    mv "$FIXTURE/threads.tmp" "$FIXTURE/threads.json"
    tty_review 'i p1|p|q'; assert_success
    draft_is '.comments[0].classification=="UNCERTAIN" and .comments[0].status=="unstaged"'
    [ ! -e "$FIXTURE/reply-count" ]
}
