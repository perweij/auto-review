#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "automatic resume recovers failed synthesis and preserves review artifacts" {
    export MOCK_FAIL_STAGE=synthesis
    run_review; assert_failure
    saved=$(run_directory)
    reports=$(sha256sum "$saved/reviews/claude-independent.md" "$saved/reviews/codex.md")
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [[ $output == *"Automatically resuming PR #7 from $saved"* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nsynthesis\ncontributions' ]
    [ "$(sha256sum "$saved/reviews/claude-independent.md" "$saved/reviews/codex.md")" = "$reports" ]
    [ "$(find "$FIXTURE/state" -name manifest.json | wc -l)" = 1 ]
}

@test "automatic matching ignores other identities, stale snapshots and malformed manifests" {
    run_review; assert_success
    saved=$(run_directory)
    for field in repository pr git_common_dir worktree snapshot; do
        other=${saved%/*}/decoy-$field
        mkdir -p "$other"
        jq --arg field "$field" --arg base "$BASE" '
            if $field=="snapshot" then .snapshot.head=$base else .[$field]="different" end' "$saved/manifest.json" > "$other/manifest.json"
    done
    mkdir -p "${saved%/*}/broken"
    echo '{broken' > "${saved%/*}/broken/manifest.json"
    run_review; assert_success
    [[ $output == *"Automatically resuming PR #7 from $saved"* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "multiple matching runs require explicit selection without changing either run" {
    run_review; assert_success
    saved=$(run_directory)
    duplicate=${saved%/*}/duplicate
    mkdir -p "$duplicate"
    cp "$saved/manifest.json" "$duplicate/manifest.json"
    before=$(sha256sum "$saved/manifest.json" "$duplicate/manifest.json")
    run_review; assert_failure
    [[ $output == *'Multiple saved runs match'* && $output == *"$saved"* && $output == *"$duplicate"* ]]
    [ "$(sha256sum "$saved/manifest.json" "$duplicate/manifest.json")" = "$before" ]
    run_review --resume "$saved"; assert_success
}

@test "missing saved run stops without replacing an existing worktree" {
    git worktree add -q --detach "$FIXTURE/repo-review-pr-7" HEAD
    echo 'Keep me' > "$FIXTURE/repo-review-pr-7/local-note"
    run_review; assert_failure
    [[ $output == *'no matching saved run'* && $output == *--resume* ]]
    [ "$(cat "$FIXTURE/repo-review-pr-7/local-note")" = 'Keep me' ]
    [ ! -f "$FIXTURE/probes" ]
    [ ! -f "$FIXTURE/stages" ]
}

@test "foreign checkout and plain directories cannot be adopted as review worktrees" {
    mkdir -p "$FIXTURE/repo-review-pr-7"
    run_review; assert_failure
    [[ $output == *'not a Git worktree'* ]]
    git init -q "$FIXTURE/repo-review-pr-7"
    run_review; assert_failure
    [[ $output == *'different checkout'* ]]
    [ ! -f "$FIXTURE/probes" ]
}

@test "automatic resume retains changed snapshot and dirty worktree protections" {
    run_review; assert_success
    echo 'local edit' >> "$FIXTURE/repo-review-pr-7/source.txt"
    run_review; assert_failure
    [[ $output == *'Automatically resuming'* && $output == *'tracked source'* ]]
    git -C "$FIXTURE/repo-review-pr-7" restore source.txt
    git push -q "$FIXTURE/remote.git" "$BASE:refs/pull/7/head" --force
    run_review; assert_failure
    [[ $output == *'Resume snapshot changed'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}

@test "automatic resume resolves from a secondary caller worktree" {
    run_review; assert_success
    saved=$(run_directory)
    git worktree add -q --detach "$FIXTURE/secondary" HEAD
    cd "$FIXTURE/secondary"
    run_review; assert_success
    [[ $output == *"Automatically resuming PR #7 from $saved"* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
}
