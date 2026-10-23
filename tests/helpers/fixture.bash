# Bats supplies status and output when run returns.
# shellcheck disable=SC2154
setup() {
    bats_require_minimum_version 1.5.0
    PROJECT_ROOT=$(cd "$BATS_TEST_DIRNAME/.." && pwd)
    export FIXTURE="$BATS_TEST_TMPDIR/fixture"
    mkdir -p "$FIXTURE"/{bin,home,state}
    export HOME="$FIXTURE/home" XDG_STATE_HOME="$FIXTURE/state"
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$FIXTURE/gitconfig"
    export GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.com GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.com
    unset GH_REPO GH_HOST VISUAL EDITOR PR_REVIEW_MODEL_TIMEOUT PR_REVIEW_CLAUDE_BUDGET PR_REVIEW_CLAUDE_PERMISSION_MODE
    unset PR_REVIEW_AUTH_TIMEOUT PR_REVIEW_CODEX_PROBE_MODEL
    git init -q --bare "$FIXTURE/remote.git"
    git init -q -b main "$FIXTURE/checkout"
    cd "$FIXTURE/checkout" || return 1
    printf 'original\n' > source.txt
    printf '__pycache__/\n' > .gitignore
    git add .; git commit -qm base
    BASE=$(git rev-parse HEAD)
    printf 'change\n' >> source.txt
    git commit -qam feature
    HEAD_SHA=$(git rev-parse HEAD)
    git push -q "$FIXTURE/remote.git" "$BASE:refs/heads/main" HEAD:refs/pull/7/head
    git remote add upstream https://github.com/org/repo.git
    git config --global url."$FIXTURE/remote.git".insteadOf https://github.com/org/repo.git
    for tool in gh codex claude; do ln -s "$PROJECT_ROOT/tests/helpers/mock-cli" "$FIXTURE/bin/$tool"; done
    export PATH="$FIXTURE/bin:$PATH"
    CLI="$PROJECT_ROOT/bin/pr-review"
}
run_review() { run "$CLI" 7 --no-publish "$@"; }
run_directory() { find "$FIXTURE/state" -type d -name '*-history' -prune -o -name manifest.json -printf '%h\n' | head -n1; }
manifest_is() { jq -e "$1" "$(run_directory)/manifest.json" >/dev/null; }
tty_review() { run python3 "$PROJECT_ROOT/tests/helpers/interactive_runner.py" "$1" "$CLI" 7 "${@:2}"; }
# shellcheck disable=SC2154
assert_success() { if [[ $status != 0 ]]; then printf '%s\n' "$output"; return 1; fi; }
# shellcheck disable=SC2154
assert_failure() { if [[ $status == 0 ]]; then printf '%s\n' "$output"; return 1; fi; }

discussion_fixture() {
    local classification=${1:-ADDITION}
    jq -n '[{id:101,user:{login:"colleague"},body:"The retry path can charge twice. Can this happen after a timeout?",html_url:"https://github.com/org/repo/pull/7#discussion_r101",created_at:"2026-01-01T00:00:00Z",updated_at:"2026-01-01T00:00:00Z"},
      {id:102,in_reply_to_id:101,user:{login:"author"},body:"The client now reuses the same key on timeout. Does that address the concern?",html_url:"https://github.com/org/repo/pull/7#discussion_r102",created_at:"2026-01-01T01:00:00Z",updated_at:"2026-01-01T01:00:00Z"}]' > "$FIXTURE/inline.json"
    jq -n --arg class "$classification" '[{id:"T1",isResolved:($class=="REVISIT"),isOutdated:false,viewerCanReply:true,path:"source.txt",line:1,originalLine:1,comments:{totalCount:2,nodes:[{databaseId:101}]}}]' > "$FIXTURE/threads.json"
    jq -n '[{id:"F1",title:"Retry after process restart",validity:"CONFIRMED",evidence:"source.txt:1 uses a new key after restart",blocking:true,body_md:"The key is not persisted across process restarts."}]' > "$FIXTURE/findings.json"
    jq -n --arg class "$classification" '{contributions:[{finding_ids:["F1"],classification:$class,discussion_keys:["thread:T1"],target_key:"thread:T1",explanation:"The timeout case was addressed; restart is a distinct case.",delta:(if $class=="DUPLICATE" then "" else "The key is lost across a process restart." end),body_md:(if $class=="DUPLICATE" then "" else "Reusing the key addresses the timeout case discussed above. After a process restart, however, a new key is generated and a completed charge can run again. Persisting the key would cover this case too." end)}],validity_conflicts:[]}' > "$FIXTURE/contribution-model.json"
}

add_new_finding() {
    jq '. + [{id:"F2",title:"New arithmetic bug",validity:"CONFIRMED",evidence:"source.txt:2 adds instead of multiplying",blocking:false,body_md:"Multiplication became addition."}]' "$FIXTURE/findings.json" > "$FIXTURE/findings.tmp"
    mv "$FIXTURE/findings.tmp" "$FIXTURE/findings.json"
    jq '.contributions += [{finding_ids:["F2"],classification:"NEW",discussion_keys:[],target_key:null,explanation:"A separate issue not in the discussion",delta:"Wrong arithmetic",body_md:"Multiplication became addition."}]' "$FIXTURE/contribution-model.json" > "$FIXTURE/contributions.tmp"
    mv "$FIXTURE/contributions.tmp" "$FIXTURE/contribution-model.json"
}
