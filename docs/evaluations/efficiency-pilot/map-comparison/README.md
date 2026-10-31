# Disposable repository-map comparison

Six fresh Luna/medium jobs, 2026-10-31. Protocol is recorded in
[the two-track plan](../../../plans/owner-efficiency.md).

**Decision: keep the map experimental; do not enable it in production.** The
publication repair improved on input and command counts, but other cases did
not. Both repair arms passed. Blind review coverage remained poor. This small
single-pair-per-case screen cannot establish reliable savings or equivalence.

| Case | Arm | Input | Uncached input | Output | Commands | Seconds | Quality |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Blind review | Control | 218,393 | 54,553 | 2,321 | 5 | 27.54 | 0 supported; 1 unsupported |
| Blind review | Map | 280,758 | 43,958 | 2,007 | 6 | 26.55 | No findings |
| Cancellation repair | Control | 250,355 | 30,963 | 1,867 | 7 | 24.97 | Gates + 22 tests pass |
| Cancellation repair | Map | 253,125 | 33,989 | 1,467 | 7 | 25.39 | Gates + 23 tests pass |
| Publication repair | Control | 193,303 | 29,719 | 1,473 | 5 | 22.68 | Gates + 48 tests pass |
| Publication repair | Map | 142,863 | 28,559 | 1,413 | 4 | 20.43 | Gates + 48 tests pass |

Across all three pairs: input +2.2%, uncached input -7.6%, output -13.7%,
commands unchanged (17 per arm), model-call elapsed time -3.8%. Different token
categories have different costs; these figures do not establish subscription
quota savings. Timings exclude fixture preparation, external checks and subsequent
module tests. No automatic retries were used; internal command failures count
within each invocation. The mapped cancellation run corrected a failed test-import
command and added two tests instead of the control's one.

The blind control claimed final repairs were not re-reviewed. Source tracing of
`owner_fixes.final_review` contradicts this: the repair branch continues the loop,
which invokes the reviewer again; only a successful review marks the batch ready.
Its elaboration about the fixer's earlier blocking report did not establish a
remaining defect after that independent review. Neither arm recovered any known
reference defects. The earlier pilot found one genuine issue, illustrating run
variation rather than a stable review capability.

## Map design and integrity

Maps contain only Python paths, AST symbol signatures and line locations. Files
are ranked by explicit task path, task-word overlap and changed paths. There is
no reference-finding input. Each file has a size allowance; the whole map is capped
at 6,000 characters. Mapped prompts are the same control prompts with the map
appended. Actual map generation took 0.0759–0.2655 seconds.

Generation reads current tracked files without importing project code. Maps are
saved outside the source checkout, and regenerated for every mapped attempt.
There is no persistent index or invalidation mechanism. Unsupported/malformed,
missing and symlinked files are skipped; overall generation failure falls back
to no map. Tests cover determinism, the budget and uncommitted modifications.
This prototype does not describe non-Python or untracked files, call relationships,
or test invocation conventions. It is deliberately partial navigation guidance.

Run order was A-control, A-map, B-map, B-control, C-control, C-map. The alternating
order reduces systematic ordering effects but does not control server cache
warmth, routing or stochastic model behavior. Aggregate cache hits cannot identify
within-job versus cross-job reuse. Control prompts match the earlier pilot;
provider sessions are fresh. No matched Sol comparison was performed.

All repair controls failed on original source and passed known corrections before
model execution. Both repair arms produced the same two-line production fixes,
plus differing regression tests. External checks and full affected historical
modules passed. Manual diff inspection found no fixture weakening or unrelated
production edits. Blind review checkouts stayed clean; synthetic snapshot history
remained unchanged. Command scans found no references to hidden evaluation/gold
artifacts or later history; no MCP calls were recorded. Isolation is procedural,
not a filesystem security boundary.

Each arm directory preserves the exact prompt, map where applicable, model result
and patch. [results.json](results.json) contains source SHAs, prompt hashes, launch
arguments, timings, usage and adjudication. Full temporary events and test logs:
`/tmp/auto-review-map-comparison-i_wvkf1c`.

## Reproduce or extend

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/evaluations/efficiency-pilot/test_repo_map.py
python3 docs/evaluations/efficiency-pilot/run_comparison.py
```

The second command consumes quota for six calls and runs local checks. It requires
the same prerequisites as the parent pilot. To run one arm, use `run.py --case A`
and optionally `--repo-map` (B/C select repairs). No production defaults change.

Do not expand this trial automatically. A useful next candidate is deterministic
test preflight plus exact failure context for bounded repairs. Another candidate
is a smaller task-specific navigation aid including verified test commands. Both
need a new paired experiment; this result does not justify deploying the map.
