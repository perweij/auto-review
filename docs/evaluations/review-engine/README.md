# Shared review engine: quality-case regression

Recorded 2026-11-02 while moving PR review, `fix-issue` acceptance and Owner final
verification onto one engine (issue #27). The question is whether PR review lost
quality when its orchestration moved into `review_engine.py`. This record states what
was run, what was not, and why. Nothing here claims a run that did not happen.

## Summary

| Check | Result |
| --- | --- |
| Deterministic PR guard (`tests/test_pr_golden.py`) | Passed. Prompts, argv and cache fingerprints for one-slot, two-slot and same-provider cases equal the capture taken from the pre-extraction code |
| PR regression suites (`review.bats`, `synthesis-policy.bats`, `restart.bats`, `revisions.bats`, `test_slot_execution.py`, `test_slot_settings.py`) and the rest of `make check-backend` | Passed (735 Python tests, all 189 Bats tests) |
| `tests/evaluate-contributions.sh` (live Claude matching, 6 cases) | **Performed. 6/6 passed** through the new code |
| Live direct-path engine smoke (`tests/smoke-engine.py`, Claude Haiku and Codex Luna) | **Performed. Both passed** |
| PR4 corpus replay (`tests/replay-pr4.sh`, `C-feature-synthesised` configuration) | **Not performed.** Providers cannot start inside the independence sandbox in this container |

## Contribution matching (`tests/evaluate-contributions.sh`)

Each case runs a full `pr-review` with mock GitHub and mock reviewers and one real
Claude contribution-matching call. All six passed (`results/contributions-live.txt`
keeps the generated replies for inspection of reply quality): `paraphrased_duplicate`,
`contextual_addition`, `resolved_revisit`, `same_line_different_bug`,
`covered_in_later_reply`, `concluded_without_resolution_flag`.

The script needed two repairs before it could run here, both unrelated to the engine:

- It ran the default processing provider, which is Codex since the defaults moved. The
  matcher is Claude, so every case failed with `unknown option '-m'` (reproduced first,
  before any repair). It now passes `--processing-provider claude`.
- The fixture setup points `CLAUDE_CONFIG_DIR` and `CODEX_HOME` at fixture homes, so the
  real matcher was not authenticated. The script now restores the caller's values.

## Live engine smoke (`tests/smoke-engine.py`)

A disposable repository, an Owner session and an uncommitted candidate (a modification
and an untracked test file) were reviewed by the shared engine with one slot matching
synthesis, so the direct path with a disposable checkout. Claude Haiku (low) and Codex
`gpt-6-luna` (low) each returned a valid `passed` result against the acceptance schema,
ran their own check in scratch, left the live candidate and Git administration unchanged
and projected to a passing Owner review (`results/engine-smoke-live.json`). This is the
only live exercise of the new prompts and schema; it shows both CLIs accept them.

## PR4 replay: not performed

`tests/replay-pr4.sh` replays the frozen PR4 corpus (base `f7f0f2e`, head `f5e0321`,
discussion withheld) once through the engine with `C-feature-synthesised`'s configuration
(Claude Opus and Codex Sol at high effort, Codex synthesis) so that the result can be
compared with that case's 8 supported behavioral findings (R2, R5, R6, R7, R8, R9, R11,
R13) and 2 supported non-blocking observations (R10, R12) in `docs/evaluations/pr4`.

It was not completed. The script was validated up to the first independent reviewer: the
fixture, mock GitHub, snapshot refs, live authentication probes of both providers and
context collection all succeed, then `reviewer-1` stops with exit 1. In this container
neither provider can start inside the independence sandbox, whose `/proc` is empty by
design and cannot be replaced with a real procfs (`results/sandbox-limitation.txt`).
Because the sandbox policy is unchanged by this work, the failure is not attributable to
the engine, and the same sandbox runs the scripted reviewers in `tests/test_review_engine.py`.
A different configuration (single reviewer, or no isolation) was deliberately not
substituted: it would not be comparable to the reference case.

No adjudication of the 10 reference findings is therefore recorded, and no regression
or absence of regression in finding recall is claimed for the live PR4 case. The
deterministic guard above is the evidence that PR prompts, argv and fingerprints are
unchanged, so model-visible behavior of the PR path should be unchanged. Run the script
on a host where providers start inside bubblewrap, compare its `synthesis.json` with
`docs/evaluations/pr4/coverage-matrix.json`, and adjudicate each missed reference
finding. Model variance alone is not a regression, and that corpus is not an exhaustive
oracle; an unexplained loss of supported findings is.
