#!/usr/bin/env bats
# Each Bats test is intentionally isolated in its own subshell.
# shellcheck disable=SC2030,SC2031
load helpers/fixture

@test "end-to-end local snapshot, independent stages, exact flags and no publication" {
    run_review
    assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
    [ "$(sort -u "$FIXTURE/reviewed-heads")" = "$HEAD_SHA" ]
    [ ! -e "$FIXTURE/post-count" ]
    manifest_is '.status == "complete" and .phases.synthesis.status == "completed"'
    manifest_is '.configuration | .claude_model=="opus" and .codex_model=="gpt-6.1-sol" and .processing_provider=="codex" and .claude_effort=="high" and .codex_effort=="high"'
    jq -e '.findings | all(.[]; .severity == "Medium")' "$(run_directory)/reviews/synthesis.json"
    grep -q 'Codex' "$(run_directory)/reviews/codex.md"
    grep -q 'codex.md' "$FIXTURE/synthesis-stdin"
    grep -q 'pr-discussion.json' "$FIXTURE/synthesis-stdin"
    jq -se 'any(.[]; .tool == "claude" and (.args | index("/code-review high --max-findings default refs/pr-review/7/base")))' "$FIXTURE/calls.jsonl"
    jq -se 'any(.[]; .tool == "codex" and (.args | index("sandbox_mode=danger-full-access")) and (.args | index("--ephemeral")) and (.args | index("--strict-config")))' "$FIXTURE/calls.jsonl"
    jq -se 'all(.[] | select((.args | join(" ") | contains("PR_REVIEW_AUTH_OK")) | not);
        .args | all(.[]; test("^--(fix|comment|bare|setting-sources|strict-mcp-config)$|^--dangerously|^checks$|check-runs") | not))' "$FIXTURE/calls.jsonl"
    [ -z "$(git -C "$FIXTURE/repo-review-pr-7" status --porcelain)" ]
    [ -z "$(git for-each-ref --format='%(refname)' refs/remotes/)" ]
    [ "$(stat -c %a "$(run_directory)/manifest.json")" = 600 ]
}

@test "each CLI authentication failure stops before checkout or models" {
    for tool in gh codex claude; do
        export MOCK_AUTH_FAIL=$tool
        run_review
        assert_failure
        [[ $output == *authentication*failed* ]]
        [ ! -d "$FIXTURE/repo-review-pr-7" ]
        [ ! -f "$FIXTURE/stages" ]
    done
}

@test "inaccessible PR and discussion stop preflight" {
    export MOCK_PR_INACCESSIBLE=1
    run_review; assert_failure
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
    unset MOCK_PR_INACCESSIBLE
    export MOCK_COMMENTS_FAIL=1
    run_review; assert_failure
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
}

@test "failing and pending CI are informational" {
    export MOCK_CI=failing
    run_review; assert_success
    [[ $output == *'1 failed, 1 pending'* ]]
}

@test "fork with multiple remotes selects target, duplicate target is ambiguous" {
    git remote add origin git@github.com:someone/fork.git
    run_review; assert_success
    [[ $output == *'Remote: upstream'* ]]
    git remote add duplicate ssh://git@github.com/org/repo.git
    run_review; assert_failure
    [[ $output == *'found 2'* ]]
}

@test "discovery failure suggests override and URL implies repository" {
    export MOCK_DISCOVERY_FAIL=1
    run_review; assert_failure
    [[ $output == *--repo* ]]
    run "$CLI" https://github.com/org/repo/pull/7 --no-publish
    assert_success
}

@test "existing worktree automatically resumes its run; existing branch name does not matter" {
    git branch repo-review-pr-7
    run_review; assert_success
    run_review; assert_success
    [[ $output == *'Automatically resuming PR #7'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
    [ "$(git branch --show-current)" = main ]
}

@test "Codex failure preserves independent Claude and exit status" {
    export MOCK_FAIL_STAGE=codex
    run_review; assert_failure
    manifest_is '.phases.codex.status == "failed" and .phases.codex.exit_status == 42'
    [ -s "$(run_directory)/reviews/claude-independent.md" ]
    [ ! -f "$(run_directory)/reviews/synthesis.json" ]
}

@test "Claude failure prevents Codex and synthesis" {
    export MOCK_FAIL_STAGE=claude
    run_review; assert_failure
    [ "$(cat "$FIXTURE/stages")" = claude ]
    manifest_is '.phases.claude.status == "failed"'
}

@test "Claude result error is a failure despite process exit zero" {
    export MOCK_CLAUDE_ERROR=1
    run_review; assert_failure
    [ "$(cat "$FIXTURE/stages")" = claude ]
}

@test "synthesis failure resumes completed independent phases" {
    export MOCK_FAIL_STAGE=synthesis
    run_review; assert_failure
    unset MOCK_FAIL_STAGE
    run_review --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nsynthesis\ncontributions' ]
    manifest_is '.phases.synthesis.status == "completed"'
}

@test "stdout-only synthesis error points to structured output and retains reviews" {
    export MOCK_STDOUT_ERROR_STAGE=synthesis
    run_review --processing-provider claude; assert_failure
    [[ $output == *'CLI result subtype: error_during_execution'* ]]
    [[ $output == *'logs/synthesis.raw.json'* && $output == *'Captured stderr is empty:'* ]]
    [[ $output == *'model: opus'* && $output != *'fixture-provider-error-do-not-replay'* ]]
    manifest_is '.phases.synthesis.status=="failed" and .phases.synthesis.exit_status==1 and (.phases.synthesis.stdout_log|endswith("logs/synthesis.raw.json"))'
    [ -s "$(run_directory)/reviews/codex.md" ]
    [ -s "$(run_directory)/reviews/claude-independent.md" ]
    [ ! -s "$(run_directory)/logs/synthesis.stderr.log" ]
    unset MOCK_STDOUT_ERROR_STAGE
    run_review --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nsynthesis\ncontributions' ]
}

@test "expired Claude OAuth is diagnosed despite success subtype and empty stderr" {
    export MOCK_STDOUT_OAUTH_STAGE=synthesis
    run_review --processing-provider claude; assert_failure
    [[ $output == *'CLI result subtype: success'* && $output == *'is_error=true'* ]]
    [[ $output == *"Claude's OAuth session expired"* && $output == *'claude auth login'* ]]
    [[ $output == *'Captured stderr is empty:'* ]]
    manifest_is '.phases.synthesis.failure_reason=="oauth_session_expired" and .phases.synthesis.status=="failed"'
    unset MOCK_STDOUT_OAUTH_STAGE
    run_review --resume "$(run_directory)"; assert_success
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\nsynthesis\ncontributions' ]
}

@test "invalid structured synthesis is retained and resumable" {
    export MOCK_SCHEMA_INVALID=1
    run_review --processing-provider claude; assert_failure
    [[ $output == *'violates the JSON schema'* ]]
    [ -s "$(run_directory)/logs/synthesis.raw.json" ]
    unset MOCK_SCHEMA_INVALID
    run_review --resume "$(run_directory)"; assert_success
}

@test "tracked source modification stops subsequent stages and publication" {
    export MOCK_MUTATE_STAGE=claude
    run_review; assert_failure
    [[ $output == *'publication blocked'* ]]
    [ "$(cat "$FIXTURE/stages")" = claude ]
    manifest_is '.integrity_failed == true and .phases.claude.status == "failed"'
}

@test "index and HEAD mutations are detected" {
    export MOCK_INDEX_STAGE=claude
    run_review; assert_failure
    manifest_is '.integrity_failed == true'
    unset MOCK_INDEX_STAGE
    git -C "$FIXTURE/repo-review-pr-7" reset --hard "$HEAD_SHA"
    export MOCK_HEAD_STAGE=claude
    run_review --resume "$(run_directory)"; assert_failure
    [[ $output == *'publication blocked'* ]]
}

@test "ignored model files are retained and disclosed" {
    export MOCK_GENERATE_STAGE=claude
    run_review; assert_success
    manifest_is '.generated_files | any(.[]; contains("__pycache__"))'
    grep -q __pycache__ "$(run_directory)/reviews/synthesis.md"
    [ -s "$FIXTURE/repo-review-pr-7/__pycache__/compiled" ]
}

@test "changing private snapshot refs blocks subsequent stages" {
    export MOCK_REF_STAGE=claude
    run_review; assert_failure
    manifest_is '.integrity_failed == true'
    [ "$(cat "$FIXTURE/stages")" = claude ]
}

@test "unrecorded untracked files prevent resume reuse" {
    run_review; assert_success
    echo unknown > "$FIXTURE/repo-review-pr-7/new-file"
    run_review --resume "$(run_directory)"; assert_failure
    [[ $output == *'unrecorded untracked/ignored files'* ]]
}

@test "missing Actions and inline comments remain unavailable, not empty successes" {
    export MOCK_ACTIONS_FAIL=1 MOCK_INLINE_FAIL=1
    run_review; assert_success
    [ "$(cat "$(run_directory)/context/ci-actions-runs.json")" = null ]
    grep -q 'ci-actions-runs unavailable' "$(run_directory)/reviews/synthesis.md"
    grep -q 'pr-inline-comments unavailable' "$(run_directory)/reviews/synthesis.md"
    run ! grep -E 'check-runs|"checks"' "$FIXTURE/calls.jsonl"
}

@test "credentials are filtered by pattern with separate CLI allowlists" {
    export GH_TOKEN=fixture-secret-github GIT_TOKEN_X=fixture-secret-git FOO_SECRET=fixture-secret-foo BAR_API_KEY=fixture-secret-bar
    export lowercase_password=fixture-secret-lower SERVICE_KEY_ID=fixture-secret-service
    export OPENAI_API_KEY=fixture-secret-openai ANTHROPIC_API_KEY=fixture-secret-anthropic
    export MOCK_REQUIRE_GH_AUTH=1
    run_review; assert_success
    for stage in claude codex synthesis contributions; do
        run ! grep -Ex 'GH_TOKEN|GIT_TOKEN_X|FOO_SECRET|BAR_API_KEY|lowercase_password|SERVICE_KEY_ID' "$FIXTURE/environment-$stage"
    done
    grep -qx OPENAI_API_KEY "$FIXTURE/environment-codex"
    run ! grep -qx ANTHROPIC_API_KEY "$FIXTURE/environment-codex"
    grep -qx ANTHROPIC_API_KEY "$FIXTURE/environment-claude"
    run ! grep -qx OPENAI_API_KEY "$FIXTURE/environment-claude"
    run ! grep -R 'fixture-secret-' "$(run_directory)"
    manifest_is '.removed_environment.claude | index("GH_TOKEN")'
}

@test "non-TTY disables menu and publication even with no no-publish flag" {
    run "$CLI" 7
    assert_success
    [[ $output != *'Choose action'* ]]
    [ ! -e "$FIXTURE/post-count" ]
}

@test "agent configuration changes stop noninteractive reviews" {
    mkdir .claude
    echo '{}' > .claude/settings.json
    echo 'trusted instructions' > AGENTS.md
    git add .; git commit -qm config
    git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
    run_review; assert_failure
    [[ $output == *AGENTS.md* && $output == *'.claude/settings.json'* ]]
    [ ! -e "$FIXTURE/stages" ]
}

@test "slow silent model has no default timeout and completes" {
    export MOCK_SLEEP_STAGE=claude MOCK_SLEEP_SECONDS=6
    run_review; assert_success
    [[ $output == *'since last output'* ]]
}

@test "explicit optional model timeout fails resumably" {
    export MOCK_SLEEP_STAGE=claude MOCK_SLEEP_SECONDS=10 PR_REVIEW_MODEL_TIMEOUT=1
    run_review; assert_failure
    manifest_is '.phases.claude.exit_status == 124'
}

@test "Ctrl-C marks active stage interrupted and permits resume" {
    export MOCK_SLEEP_STAGE=claude MOCK_SLEEP_SECONDS=4
    run python3 "$PROJECT_ROOT/tests/helpers/interrupt_runner.py" "$CLI" 7 --no-publish
    [ "$status" = 130 ]
    manifest_is '.phases.claude.status == "interrupted" and .phases.claude.exit_status == 130'
    [ -f "$(run_directory)/reviews/claude-independent.jsonl" ]
    unset MOCK_SLEEP_STAGE
    run_review --resume "$(run_directory)"; assert_success
}

@test "missing CLI capabilities stop before checkout" {
    rm "$FIXTURE/bin/codex"
    printf '#!/usr/bin/env bash\necho old-cli\n' > "$FIXTURE/bin/codex"
    chmod +x "$FIXTURE/bin/codex"
    run_review; assert_failure
    [[ $output == *'lacks required capability'* ]]
    [ ! -d "$FIXTURE/repo-review-pr-7" ]
}

@test "enterprise host is carried through repository and API calls" {
    export MOCK_HOST=git.example.com
    git remote set-url upstream ssh://git@git.example.com/org/repo.git
    git config --global url."$FIXTURE/remote.git".insteadOf ssh://git@git.example.com/org/repo.git
    run "$CLI" https://git.example.com/org/repo/pull/7 --no-publish
    assert_success
    jq -se '[.[] | select(.tool == "gh" and .args[0] == "api" and (.args | index("--help") | not))] | all(.[]; .args | index("git.example.com"))' "$FIXTURE/calls.jsonl"
}

@test "secondary caller worktree resolves main checkout location" {
    git worktree add -q --detach "$FIXTURE/secondary" HEAD
    cd "$FIXTURE/secondary"
    run_review; assert_success
    manifest_is '.worktree | endswith("/repo-review-pr-7")'
    [ -d "$FIXTURE/repo-review-pr-7" ]
    [ "$(git rev-parse HEAD)" = "$HEAD_SHA" ]
}

@test "lock prevents concurrent run" {
    mkdir -p .git/pr-review-locks
    exec {test_lock}>.git/pr-review-locks/7.lock
    flock "$test_lock"
    run_review; assert_failure
    [[ $output == *'Another operation is using this review'* ]]
    exec {test_lock}>&-
}

@test "completed run resumes without rerunning reviews, preserving edited body" {
    run_review; assert_success
    echo 'Human edited body' > "$(run_directory)/publication/review-body.md"
    run_review; assert_success
    [[ $output == *'Automatically resuming PR #7'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions' ]
    grep -q 'Human edited body' "$(run_directory)/publication/review-body.md"
}

@test "resume restarts changed independent inputs and updates a changed head" {
    run_review; assert_success
    run_review --resume "$(run_directory)" --claude-model different; assert_success
    [[ $output == *'Restarting reviews from claude'* ]]
    [ "$(cat "$FIXTURE/stages")" = $'claude\ncodex\nsynthesis\ncontributions\nclaude\ncodex\nsynthesis\ncontributions' ]
    git push -q "$FIXTURE/remote.git" "$BASE:refs/pull/7/head" --force
    run_review --resume "$(run_directory)"; assert_success
    [[ $output == *'Updating this review'* ]]
}
