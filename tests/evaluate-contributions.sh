#!/usr/bin/env bash
# Opt-in semantic evaluation. Only the matching invocation uses a real model.
set -Eeuo pipefail
umask 077
EVAL_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EVAL_HOME=$HOME EVAL_PATH=$PATH
EVAL_CLAUDE=$(command -v claude)
EVAL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pr-review-evaluation-XXXXXX")
printf 'Semantic evaluation artifacts (retained): %s\n' "$EVAL_DIR"
# Reuse repository fixtures, without needing Bats or replacing real CLI auth HOME.
bats_require_minimum_version() { :; }
# shellcheck source=helpers/fixture.bash
source "$EVAL_ROOT/tests/helpers/fixture.bash"
failures=0
while IFS= read -r case_json; do
    name=$(jq -r .name <<< "$case_json")
    if [[ $# -gt 0 && $1 != "$name" ]]; then continue; fi
    printf '\nEvaluating: %s\n' "$name"
    if (
        export BATS_TEST_DIRNAME="$EVAL_ROOT/tests" BATS_TEST_TMPDIR="$EVAL_DIR/$name"
        export PATH="$EVAL_PATH"
        setup || exit
        export HOME="$EVAL_HOME" MOCK_REAL_MATCHER="$EVAL_CLAUDE"
        discussion_fixture ADDITION
        rm "$FIXTURE/contribution-model.json"
        jq -r .source <<< "$case_json" > source.py
        git add source.py; git commit -qm "Semantic fixture: $name"
        git push -q "$FIXTURE/remote.git" HEAD:refs/pull/7/head
        jq -n --argjson case "$case_json" '[{id:"F1",title:$case.title,validity:"CONFIRMED",evidence:$case.evidence,blocking:true,body_md:$case.evidence}]' > "$FIXTURE/findings.json"
        jq --argjson case "$case_json" '.[0].body=$case.root | .[1].body=$case.reply' "$FIXTURE/inline.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/inline.json"
        jq --argjson case "$case_json" '.[0].isResolved=$case.resolved | .[0].path="source.py"' "$FIXTURE/threads.json" > "$FIXTURE/new.json"; mv "$FIXTURE/new.json" "$FIXTURE/threads.json"
        "$CLI" 7 --no-publish || exit
        jq -e --arg expected "$(jq -r .expected <<< "$case_json")" '.contributions[0].classification==$expected and (.validity_conflicts|length)==0' "$(run_directory)/reviews/contributions.json" || exit
        jq -r '.quality_check' <<< "$case_json"
        jq -r '.contributions[0].body_md' "$(run_directory)/reviews/contributions.json"
    ); then printf 'PASS: %s\n' "$name"; else printf 'FAIL: %s\n' "$name"; failures=$((failures+1)); fi
done < <(jq -c '.[]' "$EVAL_ROOT/tests/fixtures/conversations.json")
printf '\nFailures: %s. Inspect contextual reply quality in the retained artifacts.\n' "$failures"
((failures==0))
