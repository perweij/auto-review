#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

advance_head() {
    git commit --allow-empty -qm 'Update PR'
    UPDATED_HEAD=$(git rev-parse HEAD)
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
}

@test "unedited old review is archived and replaced without comparison or carried selections" {
    run_review; assert_success
    saved=$(run_directory)
    old_id=$(jq -r .run_id "$saved/manifest.json")
    jq '.proposals[0].selected=false | del(.main_generated_hash)' "$saved/publication/contributions.json" > "$FIXTURE/ledger"
    cp "$FIXTURE/ledger" "$saved/publication/contributions.json"
    # Update the generated main body to reflect the old unselected proposal.
    run_review; assert_success
    saved=$(run_directory)
    echo 'keep local notes' > "$FIXTURE/repo-review-pr-7/notes"
    advance_head
    git update-ref refs/pr-review/7/head "$UPDATED_HEAD"
    run_review; assert_success
    saved=$(run_directory)
    [[ $output == *'Updating this review'* && $output != *'Matching findings against the previous'* ]]
    archive=$(jq -r .previous_review.archive "$saved/manifest.json")
    old_worktree=$(jq -r '.snapshot_history[0].worktree' "$saved/manifest.json")
    [ "$(git -C "$old_worktree" rev-parse HEAD)" = "$HEAD_SHA" ]
    [ "$(cat "$old_worktree/notes")" = 'keep local notes' ]
    [ ! -e "$FIXTURE/repo-review-pr-7/notes" ]
    [ "$(git -C "$FIXTURE/repo-review-pr-7" rev-parse HEAD)" = "$UPDATED_HEAD" ]
    [ -s "$archive/reviews/synthesis.json" ]
    [ "$(jq -r .run_id "$saved/manifest.json")" != "$old_id" ]
    jq -e '.proposals[0].selected==true and .proposals[0].revision==null' "$saved/publication/contributions.json"
    [ ! -f "$saved/publication/previous-edits.md" ]
    [ ! -f "$saved/reviews/revision.json" ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "earlier human edits can be viewed but are never inserted into the fresh draft" {
    cat > "$FIXTURE/editor" <<'SH'
#!/usr/bin/env bash
printf 'Human-written text for the old commit.\n' > "$1"
SH
    chmod +x "$FIXTURE/editor"
    export EDITOR=$FIXTURE/editor
    tty_review 'e p1|e|n'; assert_success
    saved=$(run_directory)
    # Exercise old runs without explicit generated-body tracking, too.
    jq 'del(.main_generated_hash)' "$saved/publication/contributions.json" > "$FIXTURE/ledger"
    cp "$FIXTURE/ledger" "$saved/publication/contributions.json"
    advance_head
    jq 'map(.body_md="Fresh finding body")' "$FIXTURE/latest-findings.json" > "$FIXTURE/findings.json"
    run_review; assert_success
    saved=$(run_directory)
    [[ $output == *'Earlier edited drafts are available'* ]]
    grep -q 'Edited proposals' "$saved/publication/previous-edits.md"
    grep -q 'Edited main draft' "$saved/publication/previous-edits.md"
    grep -q 'Human-written text' "$saved/publication/previous-edits.md"
    [ "$(cat "$saved/publication/review-body.md")" = 'Fresh finding body' ]
    jq -e '.proposals[0].body_md=="Fresh finding body"' "$saved/publication/contributions.json"
    stages=$(cat "$FIXTURE/stages")
    tty_review 'u|n'; assert_success
    [[ $output == *'Human-written text'* && $output == *'No text has been copied automatically'* ]]
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "unchanged editor text is not misidentified as an edited proposal" {
    export EDITOR=true
    tty_review 'e p1|n'; assert_success
    advance_head
    run_review; assert_success
    saved=$(run_directory)
    [ ! -f "$(run_directory)/publication/previous-edits.md" ]
}

@test "the combined-body edit hash survives ordinary resume before the PR updates" {
    run_review; assert_success
    saved=$(run_directory)
    original_hash=$(jq -r .main_generated_hash "$saved/publication/contributions.json")
    echo 'Manual main review edit' > "$saved/publication/review-body.md"
    run_review; assert_success
    saved=$(run_directory)
    [ "$(jq -r .main_generated_hash "$saved/publication/contributions.json")" = "$original_hash" ]
    advance_head
    run_review; assert_success
    saved=$(run_directory)
    grep -q 'Manual main review edit' "$saved/publication/previous-edits.md"
    [ "$(cat "$saved/publication/review-body.md")" != 'Manual main review edit' ]
}

@test "a run stopped at the removed comparison stage can resume to publication preparation" {
    run_review; assert_success
    saved=$(run_directory)
    jq '.phases.revision={status:"failed"} | .phases.claude.fingerprint="old-implementation"' "$saved/manifest.json" > "$FIXTURE/manifest"
    cp "$FIXTURE/manifest" "$saved/manifest.json"
    echo 'Earlier invalid comparison response' > "$saved/logs/revision-result.json"
    run_review; assert_success
    saved=$(run_directory)
    [[ $output != *'Invalid revision comparison'* ]]
    manifest_is '.phases.revision==null and .status=="complete"'
    archive=$(jq -r '.review_history[-1].archive' "$saved/manifest.json")
    [ "$(cat "$archive/logs/revision-result.json")" = 'Earlier invalid comparison response' ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "dirty old worktree blocks update without losing old review or files" {
    run_review; assert_success
    saved=$(run_directory)
    before=$(sha256sum "$saved/publication/review-body.md" "$saved/reviews/synthesis.json")
    echo 'unsaved change' >> "$FIXTURE/repo-review-pr-7/source.txt"
    advance_head
    run_review; assert_failure
    saved=$(run_directory)
    [[ $output == *'tracked changes'* ]]
    [ "$(sha256sum "$saved/publication/review-body.md" "$saved/reviews/synthesis.json")" = "$before" ]
    [ "$(git -C "$FIXTURE/repo-review-pr-7" rev-parse HEAD)" = "$HEAD_SHA" ]
    manifest_is '.snapshot_update==null and .snapshot_history==null'
}

@test "failed updated review resumes with its original baseline without comparison" {
    run_review; assert_success
    saved=$(run_directory)
    advance_head
    export MOCK_FAIL_STAGE=codex
    run_review; assert_failure
    saved=$(run_directory)
    archive=$(jq -r '.previous_review.archive' "$saved/manifest.json")
    [ -s "$archive/reviews/synthesis.json" ]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    saved=$(run_directory)
    [ "$(jq -r '.previous_review.archive' "$saved/manifest.json")" = "$archive" ]
    manifest_is '(.snapshot_history|length)==1 and .phases.revision==null'
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 2 ]
}

@test "interrupted snapshot update is recovered even with no canonical worktree" {
    run_review; assert_success
    saved=$(run_directory)
    advance_head
    archive=$saved-history/snapshot-interrupted
    mkdir -p "$archive"
    cp -a "$saved/." "$archive/"
    prior_worktree=$FIXTURE/repo-review-pr-7-snapshot-interrupted
    jq --arg archive "$archive" --arg worktree "$prior_worktree" --arg head "$UPDATED_HEAD" \
        '.snapshot_update={archive:$archive,baseline:$archive,worktree:$worktree,run_id:"recovered-revision",from:.snapshot,to:(.snapshot+{head:$head})}' \
        "$saved/manifest.json" > "$FIXTURE/manifest"
    cp "$FIXTURE/manifest" "$saved/manifest.json"
    git worktree move "$FIXTURE/repo-review-pr-7" "$prior_worktree"
    # The cleanup can also be interrupted between deleting and recreating logs.
    rm -rf "$saved/logs"
    run_review; assert_success
    saved=$(run_directory)
    [[ $output == *'Automatically resuming'* ]]
    manifest_is '.snapshot_update==null and .run_id=="recovered-revision" and (.snapshot_history|length)==1'
    [ "$(git -C "$prior_worktree" rev-parse HEAD)" = "$HEAD_SHA" ]
    [ "$(git -C "$FIXTURE/repo-review-pr-7" rev-parse HEAD)" = "$UPDATED_HEAD" ]
}

@test "published history is archived and actual discussion controls duplicates after update" {
    tty_review 'c|yes'; assert_success
    saved=$(run_directory)
    advance_head
    jq '{contributions:[.[]|{finding_ids:[.id],classification:"DUPLICATE",discussion_keys:["review:99"],target_key:null,explanation:"Posted on GitHub",delta:"",body_md:""}],validity_conflicts:[]}' "$FIXTURE/latest-findings.json" > "$FIXTURE/contribution-model.json"
    run_review; assert_success
    saved=$(run_directory)
    archive=$(jq -r '.previous_review.archive' "$saved/manifest.json")
    jq -e '.[0].status=="published"' "$archive/publication/actions.json"
    jq -e 'length==0' "$saved/publication/actions.json"
    jq -e '.proposals[0].classification=="DUPLICATE" and .proposals[0].selected==false' "$saved/publication/contributions.json"
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
}

@test "another head update after an incomplete review retains every earlier archive" {
    run_review; assert_success
    saved=$(run_directory)
    advance_head
    export MOCK_FAIL_STAGE=codex
    run_review; assert_failure
    saved=$(run_directory)
    baseline=$(jq -r .previous_review.archive "$saved/manifest.json")
    advance_head
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    saved=$(run_directory)
    [ "$(jq -r ' .snapshot_history[0].archive' "$saved/manifest.json")" = "$baseline" ]
    manifest_is '(.snapshot_history|length)==2 and .phases.revision==null'
}

@test "merge-base change updates review even when the head stays the same" {
    run_review; assert_success
    saved=$(run_directory)
    git --git-dir="$FIXTURE/remote.git" update-ref refs/heads/main "$HEAD_SHA"
    run_review; assert_success
    saved=$(run_directory)
    manifest_is '.snapshot.merge_base==.snapshot.head and (.snapshot_history|length)==1'
    [ "$(grep -c '^revision$' "$FIXTURE/stages")" = 0 ]
}

@test "fresh review works when the prior run failed before creating proposals" {
    export MOCK_FAIL_STAGE=contributions
    run_review; assert_failure
    saved=$(run_directory)
    [ -s "$saved/reviews/synthesis.json" ]
    [ ! -f "$saved/publication/contributions.json" ]
    advance_head
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    saved=$(run_directory)
    jq -e '.proposals[0].selected==true' "$saved/publication/contributions.json"
    manifest_is '.phases.revision==null'
}
