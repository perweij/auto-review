# Specification planning evaluation — 2026-11-02

Exploratory, paired comparison of the previous planner (`control`, frozen) with the
strengthened planner on four small/complex tasks, each under an economical and a default
implementation configuration. **Read the limitations before drawing any conclusion.**
Design, arms, metrics and reproduction commands: [PROTOCOL.md](PROTOCOL.md).
Frozen settings: [manifest.json](manifest.json).

## Result in one paragraph

Once a plan existed, both arms produced implementations that passed every independent
acceptance and regression check on all four tasks under both configurations (8 pairs, 16 runs;
control 8/8 and strengthened 8/8 correct). The evidence therefore shows **no measurable quality
gain** from the strengthened planning on these tasks: the tasks were within reach of both
specification styles (a ceiling effect), and the only differences in repair behavior are one
repair round (control, `export`, economical). The strengthened planner cost more: about
1.8-2.1x the planning wall time and roughly 1.4x the planning input tokens (about 2.7x the output tokens)
per plan; implementation input tokens were mixed (higher with the strengthened plan in 5 of 8 pairs,
lower in 2, about equal in 1; see tables). The first (pre-registered) round of the strengthened planner **failed 3 of 4 tasks at
planning** by asking edge-case questions the issue text already settled; after that finding the
prompt was tuned and a second round was run. Nothing here supports a claim of improved
quality or economy, and nothing here is evidence about subscription quota.

## What was run

| Round | Runs | Planner / prompt | Notes |
| --- | --- | --- | --- |
| 1 (pre-registered) | 16 | control and strengthened, as frozen in the manifest | all 16 pairs recorded, none dropped |
| control retry | 2 | control, `recurring` only | round-1 control planning for `recurring` failed on a provider error; re-run |
| 2 (follow-up) | 8 | strengthened with the tuned prompt (planner code and validation unchanged) | one run per task and configuration; control reused from round 1 / retry |

The round-1 prompt is archived in `treatments/strengthened-round1/`. The tuning (ask only about
a *consequential* product choice; record minor edge cases as visible assumptions) was made **after
seeing round-1 results**, so round 2 is not a blind test of a pre-registered change; the control
was not tuned.

Models (all Codex, so usage is uniformly provider-reported): planning `gpt-6.1-sol`/high;
implementation economical `gpt-6.1-terra`/low and default `gpt-6.1-sol`/high; independent review
`gpt-6.1-sol`/high for every run. Claude Code was not part of the matrix (see limitations); a
separate live check that **both** providers read the handed file is in `results/handoff-smoke.json`.

## Round-1 pairs and follow-ups

Columns: outcome (stop reason); acceptance checks passed; repair rounds requested by the
reviewer; planning seconds / implementation+review seconds; provider-reported input tokens
for planning / implementation / review. Planning is done once per task and arm and shared by
both configurations; its cost therefore appears in both rows. `unavailable` = no provider usage
record (the run never reached that stage); tokens are never converted to quota.

| Pair | Arm | Outcome (stop) | Acceptance | Repairs | Plan / impl+review s | Input tokens plan / impl / review |
| --- | --- | --- | ---: | ---: | ---: | --- |
| category-case/default | control (round 1) | completed (passed_review) | 5/5 | 0 | 24 / 83 | 61,621 / 131,511 / 79,532 |
| category-case/default | strengthened round 1 | failed (unanswered_questions) | 0/5 | 0 | 32 / 0 | unavailable |
| category-case/default | strengthened round 2 | completed (passed_review) | 5/5 | 0 | 43 / 63 | 91,520 / 92,301 / 79,133 |
| category-case/economical | control (round 1) | completed (passed_review) | 5/5 | 0 | 24 / 55 | 61,621 / 106,814 / 92,149 |
| category-case/economical | strengthened round 1 | failed (unanswered_questions) | 0/5 | 0 | 32 / 0 | unavailable |
| category-case/economical | strengthened round 2 | completed (passed_review) | 5/5 | 0 | 43 / 46 | 91,520 / 110,131 / 73,928 |
| export/default | control (round 1) | completed (passed_review) | 8/8 | 0 | 49 / 134 | 114,255 / 150,701 / 86,079 |
| export/default | strengthened round 1 | completed (passed_review) | 8/8 | 0 | 100 / 172 | 143,558 / 152,535 / 96,867 |
| export/default | strengthened round 2 | completed (passed_review) | 8/8 | 0 | 92 / 177 | 144,364 / 149,911 / 79,316 |
| export/economical | control (round 1) | completed (passed_review) | 8/8 | 1 | 49 / 125 | 114,255 / 404,701 / 189,588 |
| export/economical | strengthened round 1 | completed (passed_review) | 8/8 | 0 | 100 / 89 | 143,558 / 115,163 / 112,514 |
| export/economical | strengthened round 2 | completed (passed_review) | 8/8 | 0 | 92 / 93 | 144,364 / 112,310 / 104,149 |
| list-limit/default | control (round 1) | completed (passed_review) | 4/4 | 0 | 28 / 70 | 64,476 / 92,429 / 74,427 |
| list-limit/default | strengthened round 1 | failed (unanswered_questions) | 0/4 | 0 | 44 / 0 | unavailable |
| list-limit/default | strengthened round 2 | completed (passed_review) | 4/4 | 0 | 51 / 95 | 69,865 / 122,117 / 100,344 |
| list-limit/economical | control (round 1) | completed (passed_review) | 4/4 | 0 | 28 / 51 | 64,476 / 104,843 / 74,149 |
| list-limit/economical | strengthened round 1 | failed (unanswered_questions) | 0/4 | 0 | 44 / 0 | unavailable |
| list-limit/economical | strengthened round 2 | completed (passed_review) | 4/4 | 0 | 51 / 64 | 69,865 / 128,926 / 89,826 |
| recurring/default | control (round 1) | failed (planning_failed) | 0/10 | 0 | 1130 / 0 | unavailable |
| recurring/default | control retry | completed (passed_review) | 10/10 | 0 | 30 / 117 | 49,401 / 165,437 / 105,282 |
| recurring/default | strengthened round 1 | failed (unanswered_questions) | 0/10 | 0 | 40 / 0 | unavailable |
| recurring/default | strengthened round 2 | completed (passed_review) | 10/10 | 0 | 63 / 165 | 89,982 / 182,494 / 117,943 |
| recurring/economical | control (round 1) | failed (planning_failed) | 0/10 | 0 | 1130 / 0 | unavailable |
| recurring/economical | control retry | completed (passed_review) | 10/10 | 0 | 30 / 87 | 49,401 / 113,898 / 105,414 |
| recurring/economical | strengthened round 1 | failed (unanswered_questions) | 0/10 | 0 | 40 / 0 | unavailable |
| recurring/economical | strengthened round 2 | completed (passed_review) | 10/10 | 0 | 63 / 91 | 89,982 / 119,505 / 133,880 |

### Totals (8 runs per arm; failures included)

| Arm | Correct runs | Acceptance checks | Repair rounds |
| --- | ---: | ---: | ---: |
| control, round 1 as recorded | 6 / 8 | 34 / 54 | 1 |
| strengthened, round 1 as recorded | 2 / 8 | 16 / 54 | 0 |
| control, round 1 with `recurring` replaced by the retry | 8 / 8 | 54 / 54 | 1 |
| strengthened, round 2 | 8 / 8 | 54 / 54 | 0 |

Planning, per task (one plan; wall seconds / input tokens / output tokens):

| Task | control | strengthened round 2 | strengthened round 1 |
| --- | --- | --- | --- |
| list-limit | 28 s / 64,476 / 708 | 51 s / 69,865 / 2,153 | 44 s / 69,171 / 1,313, asked 1 question, failed |
| category-case | 24 s / 61,621 / 736 | 43 s / 91,520 / 1,792 | 32 s / 52,741 / 1,653, asked 1 question, failed |
| recurring | 30 s / 49,401 / 897 (retry; round-1 attempt failed after 1,130 s) | 63 s / 89,982 / 2,658 | 40 s / 79,171 / 1,412, asked 1 question, failed |
| export | 49 s / 114,255 / 1,475 (1 clarification) | 92 s / 144,364 / 3,756 (1 clarification) | 100 s / 143,558 / 4,207 (1 clarification) |

## Observations the data supports

- **Over-asking is a real cost of the first strengthened prompt.** It asked a question on three
  tasks whose issue text defined the behavior (a tie at the `--limit` cut-off, Unicode case
  folding, `--until` without `--repeat`). In a real workflow a person would answer and the
  work would proceed; here, with no human, each was a failed run. The tuned prompt resolved
  the same edge cases as visible assumptions and planned all four tasks.
- **The clarification case worked as designed in both arms**: on `export` both planners asked
  once, received the same predetermined answers, and both implementations satisfied all 8
  checks. The control did not guess the answered choices.
- **Strengthened specifications are longer** (3.0-5.3 kB versus 1.1-2.1 kB, including the visible
  assumptions) and cost more to produce. Their implementations were not better on these tasks.
- **Economical configuration**: with the control's plan `export` needed one repair round and
  about 3.5x the implementation input tokens of the strengthened-plan run (404,701 versus
  112,310-115,163). It is a single pair; one run per cell cannot separate the plan from
  run-to-run variation.
- **Failures are retained.** The control's round-1 `recurring` planning ended in a provider
  `max_output_tokens` error after 1,130 s with no usage record; it is counted as a failure in the
  round-1 totals and replaced only by the separately labelled retry.
- No run read a hidden check path (`hidden_reference_hits` is 0 in every result).

## Specification-quality audit

Mechanical measures: `python3 audit.py <run dir>`. Judgements below come from reading each
produced specification against its issue, the fixture and the independent checks.

| Criterion | control | strengthened (round 2) |
| --- | --- | --- |
| Shape | numbered edit steps ("Extend ledger/cli.py ...", "Document ... in README.md", "Add focused tests ... then run ...") followed by acceptance bullets | the standard's sections; conditional sections only where applicable (no sections on `category-case`'s interfaces, for example) |
| Unsupported assumptions | none stated and none declared; unstated defaults are invisible to the approver (for example both `recurring` and `list-limit` plans silently decide tie and ordering behavior) | decisions surfaced under "Assumptions and resolved choices" and open to veto; a few are the planner's inventions (for example `recurring`: future occurrences always included, chronological listing of generated entries), flagged but not issue-derived |
| Scope growth | adds README documentation and tests as steps | adds documentation, help text and a regression suite to scope; comparable, slightly more explicit |
| Missing product decisions | `export` was clarified; other tasks relied on the issue text | round 1 over-asked; round 2 asked only on `export` |
| Brittle prescriptions | per-file edit steps and exact test commands (for example `python -m unittest discover -s tests`) | behavior-level, with implementation structure explicitly delegated; still names exact test commands and test files in "Verification" (`python -B -m unittest ...`) |
| Repository grounding | cites files by path; rarely records what they showed | `inspected` entries (path plus finding) validated to exist; plans cite existing components and say what the baseline suite shows |

The audit is one reader's judgement of eight documents, not an inter-rater measurement.

## Verification

- `python3 evaluate.py verify-checks`: every check set fails or only partly passes the baseline and passes a
  known-correct reference (baseline passing counts: list-limit 1/4, category-case 3/5, recurring 3/10, export 3/8; the passing ones are
  mostly usage-error guards that an unchanged CLI already satisfies).
- `original_tests_pass` correction: the check used during the live runs tested the frozen baseline package instead of
  the candidate (see [PROTOCOL.md](PROTOCOL.md#tasks)). After the fix, all 18 retained candidate diffs were re-scored from
  fresh baselines (`evaluate.py rescore`, `results/*/runs/*/rescore.json`): 18 of 18 correct, so the recorded outcomes stand,
  but the in-run `original_tests_pass` values in `result.json` are superseded by the re-derived ones.
- Deterministic suite (no model): `tests/test_evaluation_runner.py`, run by `make check-backend`.
- Repository regression results for this change (recorded 2026-11-02, after the live runs): `make check-backend` passed
  (shell lint, Bats suite, 851 Python unit tests including the new specification, workflow, handoff and
  evaluation-runner tests) and `make check-emacs` passed (162 ERT tests as expected, 0 unexpected). Those suites use mocked
  providers; they are independent of the model runs above. The specification feature is described in
  [../../manual.md](../../manual.md#the-specification).

## Limitations

- **n = 1 per cell, one fixture, four small synthetic tasks.** Run-to-run variation was not measured.
  With 16/16 pairs correct, the matrix cannot detect a quality difference and says nothing about harder tasks.
- **Ceiling effect.** Every completed run passed every check; the economical configuration was not weak
  enough on these tasks for planning detail to matter.
- **Round 2 was tuned after round 1** and its strengthened arm ran in a separate session from the control runs, so pair
  order alternation applies only to round 1. Round 2 and the control retry are not blind.
- **Planning reused across configurations**; planning variance is thus shared by two cells, and
  planning cost is double-counted if end-to-end rows are summed.
- **Concurrency.** Two task chains shared the machine and provider; elapsed times include contention and
  provider latency and are indicative only. A provider error cost the control 1,130 s on one planning attempt.
- **Review is a single independent reviewer call** (`gpt-6.1-sol`/high), not the production review engine; repair
  rounds measure that reviewer's requests. Hidden checks, not the reviewer, decide correctness.
- **Codex only.** Claude Code was verified for handoff only (a live nonce check), not for implementation quality.
- **Re-scored, not re-run.** The 18 candidate diffs were re-scored offline with the corrected check; the model runs themselves were not repeated.
- **Isolation of hidden checks is procedural** (outside the model workspace; event logs scanned), not a sandbox.
- **Usage** is what the provider reported per call. Reasoning tokens are reported separately and not added to
  output. Cached input is a subset of input. No token count is converted to, or implies, a subscription quota percentage;
  raw event logs stay in the local run directory.
- The evaluation does not measure the effect of the *file handoff itself*, which is common to both arms.

## Files

- `manifest.json` frozen settings, hashes and the run matrix inputs; `PROTOCOL.md` design.
- `control/` the frozen pre-change planner; `treatments/strengthened-round1/` the round-1 prompt.
- `fixture/`, `tasks/`, `checks/`, `reference/` the repository fixture, issues, independent checks and reference solutions.
- `evaluate.py`, `audit.py`, `tables.py`, `export_results.py`, `handoff_smoke.py` runner and helpers.
- `results/round1`, `results/round2`, `results/control-retry`: planner prompts, schemas and responses, specifications,
  implementation and review prompts, per-run results, candidate diffs. `results/handoff-smoke.json`.
