# Protocol

Frozen before any live run (see `manifest.json`).

## Question

Does the strengthened planner (specification standard, mandatory repository
inspection and decision identification, questions for unresolved product
choices, mechanical response validation) produce specifications that lead to
more correct, less repair-heavy autonomous implementation than the planner it
replaces? This is a hypothesis under test, not an assumption about any model.

## Arms

| Arm | Planner |
| --- | --- |
| `control` | `control/issue_planning_control.py`, a byte-for-byte copy of `lib/pr_review/issue_planning.py` at commit `2db9333` (blob `450b35a`). Its prompt, schema and validation were frozen before the change. Checked by SHA-256 on every run. |
| `strengthened` | `lib/pr_review/issue_planning.py` with `prompts/issue-specification.txt` and `lib/pr_review/specification.py`. Their SHA-256 values are recorded in the manifest. |

Both arms plan on an identical read-only checkout with the same planning
provider/model/effort and the same context shape as production (`issue`,
`comments`, `previous_plan`, `execution_blockers`, `pr_feedback`). Whatever
the planner returns is converted to the canonical document by the same
`specification.render` (equivalent to the control's own formula), written by
the same `specification.write`, and handed to the implementation provider by
the same Markdown file path. Transport is therefore lossless and identical.

## Tasks

One frozen repository fixture (`fixture/ledger`, a small expense ledger with
seven passing tests; tree digest in the manifest) and four tasks with
independent acceptance checks in `checks/` that run outside the model
workspace:

| Task | Size | Notes |
| --- | --- | --- |
| `list-limit` | small | one command option with explicit error behavior |
| `category-case` | small | behavior change that must preserve storage and legacy files |
| `recurring` | complex | model, storage format, new module, CLI, reports; compatibility with old files |
| `export` | complex | deliberately under-specified; carries the clarification case |

Every check set was verified to fail on the baseline and pass on a known
correct reference solution (`reference/`), by `evaluate.py verify-checks`.
Some usage-error checks pass on the baseline only because argparse already
rejects an unknown option; they guard against regressions rather than
discriminate. Regression expectations: the fixture's original tests (copied to a directory that contains no
`ledger` package and run against the candidate, which the check asserts is the package imported,
so neither weakening a test nor shadowing the candidate with the frozen baseline can hide a
failure), the repository's own tests as left by the model, and one task-specific regression check.

**Correction.** During the live runs `original_tests_pass` ran `unittest discover -t <fixture>`, which put the
frozen baseline package ahead of the candidate on `sys.path`; every `original_tests_pass: true` stored in
`results/*/runs/*/result.json` therefore tested the baseline, not the implementation. The check was fixed afterwards
(`checks/common.py`; `tests/test_evaluation_runner.py` shows that a broken candidate now fails it) and the 18 retained
candidate diffs were re-scored from fresh baselines with `evaluate.py rescore`: all 18 are correct, with the original
tests genuinely passing on the candidate. The re-derived values are in each run's `rescore.json`; the original
`result.json` files are kept unchanged as recorded. The acceptance checks and the task-specific and repository-test
regression checks were not affected.

## Clarification

`export` has predetermined maintainer answers (`tasks/export/task.json`). Whenever a
planner returns questions, or the implementation harness reports `needs_input`,
the same answer text is supplied once to either arm. An arm that does not ask is
never given the answers in planning. Tasks without answers cannot be clarified:
a planner that returns questions there ends planning unanswered and the run is
recorded as failed, because the evaluation has no human.

## Implementation, review and repair

The runner uses the production argv builders (`issue_harness.arguments`), the
production prompt `prompts/issue-implement.txt` and its context shape. A turn
that reports `ready` is followed by one independent review (separate provider
call, disposable copy of the repository, same model for every run, specification
file readable) that returns `passed` or `changes_requested`. Findings return to the
same implementation session (resumed by its recorded session id). The repair
allowance, implementation-turn limit, call timeout and wall-clock limit are in
`limits` and identical for every run. This single reviewer is a lighter stand-in
for the production review engine, not the engine itself.

Repair rounds are the number of review-requested repairs; continuation turns
(`working` reports), format corrections and clarification turns are recorded
separately.

## Pairing and order

Every (task, implementation configuration) is a pair: one run per arm with
identical planning, implementation, review, context and limits (`settings`
in each result; `tests/test_evaluation_runner.py` asserts pair identity). Pair
index parity alternates which arm runs first. Planning is performed once per
(task, arm) and reused by both implementation configurations; planning cost is
reported separately and added in the end-to-end totals of each run that uses it,
so summing end-to-end totals across both configurations counts a plan twice.

Two independent task chains ran concurrently on one machine, so wall-clock times
carry contention noise common to both arms but not controlled.

## Metrics

Per run: planning and implementation (including review) recorded separately and
end to end. Elapsed seconds; provider-reported usage (input, cached input, output,
reasoning output tokens, per call and summed; `unavailable` when no usage record
exists, `partial` when only some calls reported); repair rounds; acceptance coverage
and correctness from the independent checks; stop reason; files changed;
references to hidden check paths in model events. Failed, interrupted and
unplanned runs are kept. Token counts are never converted to a subscription quota
percentage.

## Specification audit

Each produced specification is audited (`audit.md`) for unsupported assumptions,
scope growth, missing product decisions and brittle implementation prescriptions,
by reading it against its issue, the fixture and the independent checks.

## Reproduce

```sh
python3 docs/evaluations/specification-planning/evaluate.py prepare        # no model calls
python3 docs/evaluations/specification-planning/evaluate.py verify-checks  # no model calls
python3 docs/evaluations/specification-planning/evaluate.py run --out /tmp/spec-eval   # live, consumes quota
python3 docs/evaluations/specification-planning/evaluate.py summarize --out /tmp/spec-eval
```

`run` is resumable (completed runs are skipped), uses no automatic retries, and
needs an authenticated Codex CLI. An interrupted planning attempt or run (Ctrl-C) is written
with status `planning_interrupted` / `interrupted` (partial provider usage kept, the partial tree
still scored by the independent checks); the command then exits 130. Re-running keeps that record as
`planning.interrupted-N.json` / `result.interrupted-N.json`, discards the half-built workspace and
repeats only that cell. A saved plan is reloaded from the exact bytes of `specification.md`
(verified against its recorded SHA-256), not from the sanitized `planning.json`. `--tasks a,b` selects a subset (used to run two
chains in parallel).
