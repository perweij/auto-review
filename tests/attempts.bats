#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

review_hashes() {
    sha256sum "$1"/reviews/{claude-independent.md,codex.md,synthesis.json,contributions.json} "$1/publication/drafts.json"
}

@test "Ctrl-C during replacement leaves completed review and edited draft in place" {
    run_review; assert_success
    previous=$(run_directory)
    printf 'My carefully edited review\n' > "$previous/publication/review-body.md"
    hashes=$(review_hashes "$previous")
    phases=$(jq -c '.phases | {claude,codex,synthesis,contributions}' "$previous/manifest.json")
    export MOCK_SLEEP_STAGE=claude MOCK_SLEEP_SECONDS=30
    run python3 "$PROJECT_ROOT/tests/helpers/interrupt_runner.py" "$CLI" 7 --no-publish --claude-model replacement
    [ "$status" = 130 ]
    candidate=$(run_directory)
    [ "$candidate" != "$previous" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ "$(jq -c '.phases | {claude,codex,synthesis,contributions}' "$previous/manifest.json")" = "$phases" ]
    [ ! -L "$previous/.current-review" ]
    [ -L "$previous/.pending-review" ]
    manifest_is '.phases.claude.status=="interrupted" and .attempt.status=="building"'
    unset MOCK_SLEEP_STAGE
    run_review --claude-model replacement; assert_success
    [ "$(run_directory)" = "$candidate" ]
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ ! -L "$previous/.pending-review" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ "$(cat "$candidate/publication/review-body.md")" = 'My carefully edited review' ]
    stages=$(cat "$FIXTURE/stages")
    run_review --claude-model replacement; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}

@test "synthesis and matching failures never install a replacement and resume checkpoints" {
    run_review; assert_success
    previous=$(run_directory)
    hashes=$(review_hashes "$previous")
    export MOCK_FAIL_STAGE=synthesis
    run_review --codex-model replacement; assert_failure
    candidate=$(run_directory)
    [ ! -L "$previous/.current-review" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    export MOCK_FAIL_STAGE=contributions
    run_review --codex-model replacement; assert_failure
    [ "$(run_directory)" = "$candidate" ]
    [ ! -L "$previous/.current-review" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 2 ]
    unset MOCK_FAIL_STAGE
    run_review --codex-model replacement; assert_success
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 2 ]
    [ "$(grep -c '^synthesis$' "$FIXTURE/stages")" = 3 ]
}

@test "changed PR head keeps last completed review current until new review succeeds" {
    run_review; assert_success
    previous=$(run_directory)
    hashes=$(review_hashes "$previous")
    git commit --allow-empty -qm 'New revision'
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    export MOCK_FAIL_STAGE=codex
    run_review; assert_failure
    candidate=$(run_directory)
    [ ! -L "$previous/.current-review" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ "$(jq -r .snapshot.head "$previous/manifest.json")" = "$HEAD_SHA" ]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [ "$(run_directory)" = "$candidate" ]
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
}

@test "interrupted promotion is completed without rerunning reviewers" {
    run_review; assert_success
    session=$(session_directory)
    run_review --codex-model replacement; assert_success
    candidate=$(run_directory)
    stages=$(cat "$FIXTURE/stages")
    # Simulate stopping immediately before the atomic pointer switch.
    rm "$session/.current-review"
    ln -s "$candidate" "$session/.pending-review"
    run_review --codex-model replacement; assert_success
    [ "$(realpath -e "$session/.current-review")" = "$candidate" ]
    [ ! -L "$session/.pending-review" ]
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
    # Simulate stopping just after switching, but before removing pending.
    ln -s "$candidate" "$session/.pending-review"
    run_review --codex-model replacement; assert_success
    [ ! -L "$session/.pending-review" ]
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}

@test "failed synthesis-only refresh preserves the completed review and independent checkpoints" {
    mkdir -p "$FIXTURE/tool"
    cp -a "$PROJECT_ROOT/bin" "$PROJECT_ROOT/lib" "$PROJECT_ROOT/prompts" "$FIXTURE/tool/"
    CLI=$FIXTURE/tool/bin/pr-review
    run_review; assert_success
    previous=$(run_directory)
    hashes=$(review_hashes "$previous")
    printf '\nCheck retry paths.\n' >> "$FIXTURE/tool/prompts/synthesis.txt"
    export MOCK_FAIL_STAGE=synthesis
    run_review; assert_failure
    candidate=$(run_directory)
    [ "$candidate" != "$previous" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ ! -L "$previous/.current-review" ]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
    stages=$(cat "$FIXTURE/stages")
    run_review; assert_success
    [ "$(cat "$FIXTURE/stages")" = "$stages" ]
}

@test "failed discussion rematch keeps old drafts and resumes only matching" {
    run_review; assert_success
    previous=$(run_directory)
    hashes=$(review_hashes "$previous")
    discussion_fixture ADDITION
    export MOCK_FAIL_STAGE=contributions
    run_review; assert_failure
    candidate=$(run_directory)
    [ "$candidate" != "$previous" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    [ ! -L "$previous/.current-review" ]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^synthesis$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^contributions$' "$FIXTURE/stages")" = 3 ]
}

@test "later failed replacement leaves the previously promoted generation current" {
    run_review; assert_success
    session=$(session_directory)
    run_review --codex-model first-replacement; assert_success
    previous=$(run_directory)
    hashes=$(review_hashes "$previous")
    export MOCK_FAIL_STAGE=codex
    run_review --codex-model second-replacement; assert_failure
    [ "$(realpath -e "$session/.current-review")" = "$previous" ]
    [ "$(run_directory)" != "$previous" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
    unset MOCK_FAIL_STAGE
    run_review --codex-model second-replacement; assert_success
    [ "$(realpath -e "$session/.current-review")" = "$(run_directory)" ]
    [ "$(review_hashes "$previous")" = "$hashes" ]
}

@test "discussion refresh preserves edits and later rematching retains them in history" {
    discussion_fixture ADDITION
    run_review; assert_success
    previous=$(run_directory)
    cat > "$FIXTURE/editor" <<'SH'
#!/usr/bin/env bash
printf 'User edit before the refresh\n' > "$1"
jq '.[1].body += " Further discussion."' "$FIXTURE/inline.json" > "$FIXTURE/next-inline.json"
mv "$FIXTURE/next-inline.json" "$FIXTURE/inline.json"
SH
    chmod +x "$FIXTURE/editor"
    export EDITOR=$FIXTURE/editor
    tty_drafts 'e p1|refresh|q'; assert_success
    draft_is '.comments[0].body=="User edit before the refresh\n"'
    export MOCK_FAIL_STAGE=contributions
    run_review; assert_failure
    candidate=$(run_directory)
    [ "$candidate" != "$previous" ]
    jq -e '.comments[0].body=="User edit before the refresh\n"' "$previous/publication/drafts.json"
    [ ! -e "$FIXTURE/post-count" ]
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [ "$(realpath -e "$previous/.current-review")" = "$candidate" ]
    draft_is '.comments[0].body!="User edit before the refresh\n" and .comments[0].status=="unstaged"'
    [ "$(grep -c '^claude$' "$FIXTURE/stages")" = 1 ]
    [ "$(grep -c '^codex$' "$FIXTURE/stages")" = 1 ]
}
