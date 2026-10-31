# Owner efficiency experiments and commit preservation

Agreed scope: evaluate small changes before changing production model defaults.
Mixed imported/published conversation linking is deferred. Do not modify PR9.

## Track 1: bounded repository-map experiment

Status: complete. Six runs finished, both repair pairs passed external gates and
affected modules. Results were mixed; production defaults remain unchanged. See
[the report](../evaluations/efficiency-pilot/map-comparison/README.md).

- Use the three frozen cases in `docs/evaluations/efficiency-pilot`: blind PR4
  review, PR8 cancellation repair, PR4 publication repair.
- Run six fresh Luna/medium jobs: one control and one mapped job per case.
  Alternate arm order across cases to reduce systematic ordering effects.
  Same source, task instructions, tools, schema and five-minute timeout per call.
  No retries or automatic expansion. This is a screening experiment, not a
  statistically reliable benchmark or a matched Sol comparison.
- Generate the map before every mapped job from current tracked Python source,
  using stdlib AST only. Include paths and symbol signatures/locations. Cap the
  map at 6,000 characters, with per-file limits to avoid one large file dominating.
  Rank files using only task words/paths and changed paths, never reference fixes.
- No persistent index, MCP service, extra dependencies or invalidation mechanism.
  Save the map with attempt artifacts, outside the checkout. Source parsing never
  executes project code. Skip unreadable/unsupported files; continue without the
  map on generation failure. Source remains authoritative after edits.
- Before paid runs, verify deterministic output, size limits, recognition of
  uncommitted edits and safe handling of malformed Python/deleted files.
- Repair gates must fail on frozen sources and pass known corrections before
  model calls. After calls, run external gates and historical affected test
  modules; inspect patches for weakened fixtures or scope drift.
- Adjudicate blind findings against source/reproductions. Count only supported
  findings; count reference coverage separately from unsupported claims.
- Save prompts, maps, patches, metrics and verdicts. Compare total/cached/uncached
  input, output, command count, duration, retries and quality. Aggregate cache
  hits do not prove cross-job reuse or subscription quota savings.
- Adoption gate: no observed quality regression, useful exploration or token
  reduction, and simple failure behavior. Mixed/negative results mean leave the
  experiment optional and production unchanged. A larger repeated trial would
  be needed before claiming reliable savings.

## Track 2: preserve task commits and verify the final tree

Status: implemented in the feature worktree and audited after the experiments.
The focused commit-plan/automatic-commit suite passed 41 tests; Emacs passed all
150 ERT tests. Lint and diff checks passed. Earlier validation of these production
changes passed all 418 Python tests; no production code changed during this
experiment turn. PR9 was not modified.

Implementation: `owner_commit_plan.py` retains continuous task boundaries and
appends integration repair, withholding item mappings for incomplete series;
`owner_commit.py` provides repair messages and sequential selection;
`owner_publication.py` enforces the complete integration series even for explicit
associations. Existing final-review and exact-tree checks remain in force.
Emacs labels the repair explicitly. Recovery and legacy fallback tests pass.

Required behavior:

1. Preserve a continuous sequence of verified task commits.
2. Append an integration-repair commit when final integration changes the tree;
   do not squash previously verified task work merely because repair was needed.
3. Require successful verification of the exact final tree before committing or
   making publication available. Require the complete relevant commit series at
   the destination before publishing replies, including manual associations.
4. Keep precise task-to-commit reply mappings, including integration repair where
   applicable; withhold incomplete-series mappings/publication.
5. Resume interrupted commits without duplication; retain compatibility with
   started legacy combined transactions. Use a documented combined fallback for
   unavailable or discontinuous task boundaries.
6. Show integration repair clearly in Emacs. Preserve the existing shared backend
   configuration. No mixed-conversation linking work in this change.

Verification: focused backend tests for commit boundaries, final-review failure,
partial commits/pushes, explicit associations, hook failure and recovery; Emacs
ERT for presentation; backend/Emacs checks as appropriate. Record final results
and any gaps. Do not push or revisit PR9 as part of this work.

## Later candidates

Luna for bounded repairs with strong verification; deterministic prechecks before
expensive review; task-specific context; optionally fixer-only session reuse.
Each needs independent measurement. Do not remove final integration review or
infer account quota savings from raw token counts.
