# Historical Owner compatibility evaluations

These experiments ran on 2026-10-31 during issue #10 dogfooding. They evaluated
bounded task repair, not the current persistent issue harness. All three model
candidates were rejected by independent review. These are historical measurements,
not outstanding recovery instructions or evidence about the completed PR #11.

## Repair-only comparison

Each arm received the same frozen configuration task, one repair and one review,
using Codex `gpt-6.1-terra` with high effort. The treatment added explicit behavioral
verification cases. External checks were withheld from the models.

| Metric | Control | Behavioral cases |
|---|---:|---:|
| External checks passed | 8/15 | 12/15 |
| Model calls | 2 | 2 |
| Input tokens | 422,283 | 804,235 |
| Uncached input tokens | 71,691 | 75,403 |
| Output tokens | 4,661 | 7,397 |
| Elapsed seconds | 66.6 | 104.5 |

Cases improved coverage but did not produce a complete repair. Both independent
reviewers rejected their candidate. Uncached input increased 5.2%; this single
sequential comparison cannot establish a general quality or efficiency trend.
Token counts do not measure subscription-quota consumption.

Measurements and per-case outcomes: [control](results/control/summary.json) and
[treatment](results/treatment/summary.json).

## Planning before implementation

A follow-up derived cases before coding and used the production task loop with
one bounded repair. It passed 15/19 external checks but was independently rejected.
Five model calls consumed 1,533,863 input tokens (195,111 uncached) in 222 seconds.
Its different starting scope and additional checks prevent a direct cost or
quality comparison with the repair-only pair.

Measurements and per-case outcomes: [planning-first](results/planned/summary.json).

## Clarified saved-choice contract

Subsequent discussion established that saved choices survive unless explicitly
overridden. Repeating an ordered reviewer list must preserve its slot models and
efforts; changing reviewers alone must not reset independent saved processing.
Explicit processing changes must not reset reviewer slots. Provisional defaults
and presets must not replace saved choices.

A separate public-API rescore checked six scenarios with providers swapped. Each
frozen candidate, including the manually patched reference, passed only 2/12:
[control](results/saved-choices/control.json),
[treatment](results/saved-choices/treatment.json),
[planning-first](results/saved-choices/planned.json), and
[manual reference](results/saved-choices/manual-reference.json).
These use a different contract and denominator from the earlier checks. They are
not a model-quality regression measurement. The manual reference was never a
complete acceptance oracle.

The general lessons remain useful: derive cases from public callers, distinguish
saved values from provisional defaults and explicit requests, and independently
check assumptions rather than accepting a passing case list as complete coverage.
The separate [Emacs follow-up](../../plans/owner-compatibility-repairs.md#issue-10-follow-up-emacs-processing-defaults-and-independent-selection)
retains the frontend observations and acceptance criteria; its current status
must be checked against the provider-independence changes.

## Archived reproduction material

[Provenance](provenance.json) records the historical source identities and model.
Full replay scripts, frozen prompts and schemas, candidate/reference patches,
Fable consultation, and the detailed development chronology are preserved in Git
at commit `9d8c12253f5bf0aeade8b889c9af4f1cd01d036b` under this directory.
For example, inspect the old protocol without modifying the checkout:

```sh
git show 9d8c12253f5bf0aeade8b889c9af4f1cd01d036b:docs/evaluations/owner-compatibility/PLANNED.md
```

Those scripts depended on the then-current backend and are no longer advertised
as runnable against today's code. Reproduce them only with their historical
checkout and explicit authorization for any paid model calls. Raw temporary
provider logs outside Git are not part of this archive.
