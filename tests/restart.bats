#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031
load helpers/fixture

invalidate_claude() {
    jq '.phases.claude.fingerprint="older-script-version"' "$saved/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$saved/manifest.json"
}

@test "invalid cache record after failed synthesis archives both reviews and restarts automatically" {
    export MOCK_FAIL_STAGE=synthesis
    run_review; assert_failure
    saved=$(run_directory)
    prior=$(cat "$saved/reviews/codex.md")
    invalidate_claude
    unset MOCK_FAIL_STAGE
    run_review; assert_success
    [[ $output == *'Restarting reviews from claude'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nclaude\ncodex\nsynthesis\ncontributions' ]
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ "$(cat "$archive/reviews/codex.md")" = "$prior" ]
    [ -s "$archive/reviews/claude-independent.md" ]
    [ -f "$archive/logs/synthesis.stderr.log" ]
    manifest_is '.review_restart==null and .phases.synthesis.status=="completed"'
    run_review; assert_success
    manifest_is '.review_history|length==1'
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "changing only Codex retains Claude but invalidates synthesis and matching" {
    run_review; assert_success
    saved=$(run_directory)
    claude_hash=$(sha256sum "$saved/reviews/claude-independent.md")
    run_review --codex-model other-codex; assert_success
    [[ $output == *'Restarting reviews from codex'* && $output == *'Reusing completed Claude review'* ]]
    [ "$(sha256sum "$saved/reviews/claude-independent.md")" = "$claude_hash" ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\ncodex\nsynthesis\ncontributions' ]
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ -s "$archive/reviews/claude-independent.md" ]
    [ -s "$archive/reviews/codex.md" ]
    [ -s "$archive/reviews/synthesis.json" ]
}

@test "damaged Claude report restarts both reviewers and retains human draft history" {
    run_review; assert_success
    saved=$(run_directory)
    echo 'Human edited body' > "$saved/publication/review-body.md"
    echo 'damaged report' > "$saved/reviews/claude-independent.md"
    run_review; assert_success
    archive=$(jq -r '.review_history[0].archive' "$(run_directory)/manifest.json")
    [ "$(cat "$archive/reviews/claude-independent.md")" = 'damaged report' ]
    [ "$(cat "$archive/publication/review-body.md")" = 'Human edited body' ]
    [ "$(cat "$saved/publication/review-body.md")" = 'Human edited body' ]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "resume finishes a partially moved restart before running independent reviewers" {
    run_review; assert_success
    saved=$(run_directory)
    archive=$saved-history/interrupted
    mkdir -p "$archive/reviews"
    # Simulate interruption after moving the Codex report but before invalidating
    # the phase records or moving the remaining files.
    jq --arg archive "$archive" --arg saved "$saved" '.review_restart={from:"claude",archive:$archive,
        files:[$saved+"/reviews/codex.md",$saved+"/reviews/claude-independent.md",$saved+"/reviews/synthesis.json",$saved+"/reviews/contributions.json"]}' \
        "$saved/manifest.json" > "$FIXTURE/manifest.tmp"
    mv "$FIXTURE/manifest.tmp" "$saved/manifest.json"
    mv "$saved/reviews/codex.md" "$archive/reviews/codex.md"
    run_review; assert_success
    [ -s "$archive/reviews/codex.md" ]
    [ -s "$archive/reviews/claude-independent.md" ]
    manifest_is '.review_restart==null and (.review_history|length)==1 and .phases.claude.status=="completed"'
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
}

@test "restart retains published history and gives replacement findings a fresh ledger" {
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    saved=$(run_directory)
    actions=$(sha256sum "$saved/publication/drafts.json")
    old_id=$(jq -r .run_id "$saved/manifest.json")
    invalidate_claude
    printf '%s\n' '[{"id":"F1","title":"A different finding","validity":"CONFIRMED","evidence":"source.txt:2 changed behavior","blocking":false,"body_md":"A different issue with the reused ID."}]' > "$FIXTURE/findings.json"
    run_review; assert_success
    candidate=$(run_directory)
    [ "$candidate" != "$saved" ]
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$actions" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    jq -e '.batches==[]' "$candidate/publication/drafts.json"
    jq -e '.proposals[0].published==false and .proposals[0].selected==false' "$candidate/publication/contributions.json"
    manifest_is '.publication.status=="not_published" and .publication.published_actions==0 and .publication_reset.status=="complete"'
    [ "$(jq -r .run_id "$candidate/manifest.json")" != "$old_id" ]
    [[ $output != *'Partially published'* ]]
    [ -s "$saved/reviews/codex.md" ]
}

@test "restart keeps uncertain publication records and refuses to discard them" {
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    saved=$(run_directory)
    jq '.batches[0].actions[0].status="submitting"' "$saved/publication/drafts.json" > "$FIXTURE/actions.tmp"
    mv "$FIXTURE/actions.tmp" "$saved/publication/drafts.json"
    actions=$(sha256sum "$saved/publication/drafts.json")
    invalidate_claude
    run_review; assert_failure
    [[ $output == *'Resolve uncertain publication'* ]]
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$actions" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    manifest_is '.review_restart==null and .publication_reset==null'
}

@test "processor-only replacement after publication retains independent reports" {
    tty_review 'a p1|overall|s p1|p|yes|q' --processing-provider claude; assert_success
    saved=$(run_directory)
    actions=$(sha256sum "$saved/publication/drafts.json")
    run_review --processing-provider codex; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nsynthesis\ncontributions' ]
    [ "$(sha256sum "$saved/publication/drafts.json")" = "$actions" ]
    [ "$(cat "$FIXTURE/post-count")" = 1 ]
    manifest_is '.configuration.processing_provider=="codex" and .publication.published_actions==0'
    jq -e '.batches==[]' "$(run_directory)/publication/drafts.json"
    tty_review 'a p1|overall|s p1|p|yes|q'; assert_success
    previous=$(run_directory)
    receipt=$(sha256sum "$previous/publication/drafts.json")
    [ "$(jq -r '.batches[0].actions[0].marker' "$previous/publication/drafts.json")" != "$(jq -r '.batches[0].actions[0].marker' "$saved/publication/drafts.json")" ]
    export MOCK_FAIL_STAGE=claude
    run_review --claude-model haiku; assert_failure
    [[ $output != *'Partially published'* ]]
    [ ! -f "$(run_directory)/publication/drafts.json" ]
    [ "$(sha256sum "$previous/publication/drafts.json")" = "$receipt" ]
    [ "$(cat "$FIXTURE/post-count")" = 2 ]
}
