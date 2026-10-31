# Owner compatibility-repair replay

2026-10-31. **Partial improvement; the autonomous-recovery acceptance criterion
did not pass.** Do not treat the manual reference or either replay as a completed
fix of issue #10 / PR #11.

The subsequent [planning-first experiment](PLANNED.md) is also complete. It passed
15/19 checks with one bounded repair but was independently rejected; live recovery
remains deferred. Its broader starting scope prevents a direct cost comparison.

## Method

Reconstructed the first configuration task immediately before its bounded repair
from the recorded Git tree and original task, approved scope, implementation
report and four review findings. Each arm received one repair and one independent
review, using Codex `gpt-6.1-terra` / high. No planner call, extra retry, escalation,
commit, push, publication or live-session modification was performed.

The control uses the existing task-scoped instructions and original output
schemas. Treatment adds the behavioral-case instructions and schemas, validates
fixer case evidence, and passes the cases to independent review. Both arms use
the production provider launcher, checkpoint and Git-integrity guards. Their
disposable repositories have no remote and contain only the historical baseline
plus the frozen uncommitted input, not later source history. A common instruction
restricts investigation to the assigned checkout and scratch. This is procedural
isolation in the existing container, not an additional OS sandbox.

The implementation model receives neither `check_cases.py` nor the manual patch.
The external checks cover explicit defaults, saved legacy configuration, implicit
processing, explicit request overrides, canonical round trips, and preservation
of the first review's findings. The original 13-case oracle was supplemented with
two planned first-review checks (generation retention and processing catalog
discovery) during the run; both were applied to every frozen result afterward.
No model input or generated source was changed based on the oracle.

## Results

| Metric | Control | Behavioral cases |
|---|---:|---:|
| External cases passed | 8 / 15 | 12 / 15 |
| Independent review | Changes requested | Changes requested |
| Model calls | 2 | 2 |
| Input tokens | 422,283 | 804,235 |
| Cached input tokens | 350,592 | 728,832 |
| Uncached input tokens | 71,691 | 75,403 |
| Output tokens | 4,661 | 7,397 |
| Command tool calls | 17 | 20 |
| Elapsed seconds | 66.6 | 104.5 |

Treatment fixed the explicit-default precedence regression for both providers
and both legacy/canonical default representations. It still failed saved legacy
selection over canonical defaults (both providers) and implicit processing for a
legacy sole-Codex configuration. All four supplied case groups passed their
focused checks, but the reviewer correctly identified omissions and rejected the
task. Per-case success did not suppress independent review findings.

The treatment reviewer also found a preset-precedence defect: single-Claude
defaults plus the economical preset produce two reviewers but Claude processing,
although the preset selects Codex. A subsequent external reproduction confirmed
this in both replay arms, the original failed repair, and the manual reference.
This was not included in the predeclared 15-case score. The manual reference
passes those 15 checks but is not a complete oracle for the whole feature.

Uncached input increased 5.2%; output increased 58.7%. Total input increased much
more because repeated input was largely cached. These are CLI token counts,
not measurements of subscription-quota usage. One sequential pair cannot establish
a quality or cost trend; cache warmth and model variability are uncontrolled.

## Interpretation and next experiment

Explicit cases made some adjacent behavior visible but did not reliably produce
a complete compatibility matrix. The legacy task began without planner cases;
the fixer largely organized its cases around the four supplied findings. This
experiment tests adding cases during repair, not the new planner's ability to
establish cases before implementation.

The revised hypothesis is that representation changes need behavioral cases
derived from public input sources and saved-state formats **before coding**, then
carried through repair. A future bounded experiment should exercise the existing
planning call at that point, checking whether it identifies defaults, presets,
saved configuration and request precedence without being given held-out answers.
Do not add retries, switch models, or claim this generic prompt can prevent all
generated-code defects. The independent rejection gate remains necessary.

Live dogfooding is deferred because the agreed replay criterion failed. The live
Owner worktree still contains the earlier manual correction. Its delta is archived
separately; reconcile it explicitly before any future live retest. Live attempt
history and consumed repair allowances remain intact.

## Program validation

`make check-all` passed: 185 CLI/Bats tests, 520 Python tests and 150 Emacs tests,
plus lint. The final focused contract suite passed all 15 tests after adding
provider-response and publication-gate coverage. Frozen-control reconstruction
and `git diff --check` also passed. These validate workflow invariants; they do
not turn either rejected model repair into a successful feature implementation.

## Reproduction and artifacts

From a checkout containing the baseline object recorded in `provenance.json`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/evaluations/owner-compatibility/run.py /tmp/new-owner-replay
# Explicitly run four model calls:
PYTHONDONTWRITEBYTECODE=1 python3 docs/evaluations/owner-compatibility/run.py /tmp/new-owner-live-replay --live
```

The first command reconstructs the failed and manual-reference controls and runs
the external checks without models. The second additionally runs the paired
repair/review experiment. Each model call has a 300-second timeout by default.
The script requires a fresh output directory and does not alter live PR state.

`results/` retains reports, generated patches, exact prompt content with temporary
paths replaced by placeholders, schemas, metrics and external verdicts. Raw
provider events and full local journals from this run are under
`/tmp/owner-compatibility-replay-1/`. Fixture patches and the manually written
reference delta are evaluator inputs only. No raw credential-bearing logs are
included in the repository.
