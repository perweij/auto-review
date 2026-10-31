# Owner efficiency pilot — 2026-10-31

Follow-up: [six-run repository-map comparison](map-comparison/README.md).

## Result and decision

Three real Codex CLI invocations used `gpt-6-luna`, medium effort. Both narrowly
specified repairs passed independent checks and their historical test modules.
The blind review found one independently reproduced behavioral issue. This supports
further narrow-repair experiments, not replacing the default reviewer or claiming
equivalent coverage. Production model settings were not changed.

| Case | Source | Outcome | Seconds | Input | Cached input | Output | Commands |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Blind review | PR4 initial head | One supported finding | 39.82 | 372,790 | 314,880 | 2,062 | 7 |
| Cancellation repair | PR8 initial head | External check + 22 tests pass | 22.44 | 226,938 | 194,688 | 1,525 | 6 |
| Publication repair | PR4 initial head | External checks + 48 tests pass | 34.50 | 372,756 | 345,088 | 1,960 | 8 |

Input is cumulative provider-reported usage across the turn, not unique source
size. Cached input is a subset of input. Reasoning output is recorded separately
in results.json; do not add it to output without checking provider semantics.
These figures do not establish ChatGPT subscription quota savings. There is no
matched Sol run with these prompts, and only two simple repair cases.

The recovered quality corpus is [PR4](../pr4/evaluation.json), rather than PR8.
Its strongest historical combined review had eight supported behavioral findings
and two observations. That is useful reference evidence, not a controlled model
comparison: prompts, providers, tools, effort and run dates differ. The reference
findings are not an exhaustive oracle. PR8 supplied the cancellation repair case.

## Protocol and adjudication

Frozen sources:

- PR4 base: `f7f0f2e2df548186b3b85d5f420ad0424af25655`.
- PR4 head: `f5e0321ca179d4c4a434ec456cb404b3cedd7486`.
- PR8 head: `51432695b33c292a316328ec944df29f843856ce`.

Each case used a separate Git repository containing archived source and synthetic
snapshot commits, with no remotes or later history. The blind review received the
whole historical diff, no reference findings. Repairs received a precise defect
statement, not an expected patch. Each repair's external check failed on the
baseline and passed on a known correction before model execution. Checks were
outside the model checkout, with instructions prohibiting their inspection.
This is procedural isolation, not a filesystem sandbox.

The blind finding is specifically that a successfully published, receipted reply
changes the discussion fingerprint, stales the batch, and unstages the next reply.
`check_review.py` reproduces this using the historical Owner test fixture and
mocked GitHub. It does not justify ignoring all comments authored by the owner.

The cancellation repair adds `except Cancelled: raise` before ordinary error
handling, and a regression test for unchanged staging and persisted state. The
publication repair checks OPEN after each refresh, before creating a write intent.
External checks cover an open batch, closure before the first or second reply,
and merge before the first reply. Earlier successful receipts survive closure.
Both patches only add regression methods to existing fixtures; fixture behavior
was not weakened. Tests and adjudication use mocked GitHub, not live publication.

The blind checkout stayed clean; repair diffs are saved alongside prompts and
model results. Commit counts remained two for review and one for each repair.
An event-command scan found no references to hidden checks, gold directories,
evaluation reports or future Git history. No model MCP calls were recorded.

Full local event logs: `/tmp/auto-review-efficiency-20261031-197imm0u`.
Sanitized metrics, exact prompts, output schema and repair patches are preserved
here; temporary logs are not required to rerun the evaluation.

## Reproduce

Requires Python 3.12+, Git, the frozen commits in this repository, and authenticated
Codex CLI for live runs. Run from this checkout. Preparation uses no model calls:

```sh
python3 docs/evaluations/efficiency-pilot/run.py --prepare-only
python3 docs/evaluations/efficiency-pilot/run.py --model gpt-6-luna --effort medium
python3 docs/evaluations/efficiency-pilot/check_review.py /absolute/artifacts/A-blind-review/repo
```

The live command consumes quota, uses a five-minute per-call timeout, and performs
three calls without automatic retries. It explicitly disables Codex sandboxing,
uses ephemeral sessions and disables app tools. It does not change global config.
For a matched baseline, rerun with `--model gpt-6.1-sol --effort high`; adjudicate
quality before comparing usage, and include failed attempts in any cost totals.

## Existing Owner batch: where to optimize

Saved PR9 attempt logs contain one planner, 16 fixer and 18 reviewer attempts
(35 starts; 34 completed turns). Totals: 33,164,856 input, 29,782,656 cached input
(89.8%), and 167,598 output tokens. The fixer accounted for 19,767,913 input;
reviewers 12,592,070; planner 804,873. These are observations of this batch, not
proof that any proposed optimization will save a particular percentage of quota.
The aggregate cache rate does not distinguish within-job prefix reuse from
cross-job reuse. A fresh job makes multiple model requests, so a high cache rate
does not establish a shared account-wide cache.

With 12 planned tasks, one planner + 12 fixes + 12 task reviews + one final review
would be 26 invocations before repairs/failures. The nine extra starts are not
necessarily avoidable: repairs can be required for correctness. Infrastructure
failures and simple fixture errors are good targets; eliminating the final
integration review is not. It caught an actual integration problem in this run.

Imported review context was about 807k serialized characters, concentrated in
14 objects covering three files. These are not identical objects: anchors differ,
and overlapping context needs deliberate consolidation. File-backed transport
avoids request-size errors but does not by itself reduce model input tokens.

Priority experiments:

1. Run a small deterministic test set before invoking the expensive verifier.
   Feed exact failing tests and relevant source to one bounded narrow-repair
   attempt, then retain strong verification and escalate on failure or scope growth.
2. Supply task-specific context plus a compact shared manifest of paths, relevant
   symbols and tests. Load additional source on demand. Key cached facts by the
   checkpoint tree and supporting file hashes, not HEAD: Owner fixes are initially
   uncommitted. Keep caches outside the source worktree so they do not interfere
   with integrity checks.
3. Compare Luna focused implementation against Sol on a larger set including
   multi-file/stateful repairs. Count repair retries, verifier calls, final-tree
   correctness and total usage, not merely the initial model call.
4. Test explicit-session continuation only within one fixer/repair sequence.
   Keep reviewers independent. Never use `--last` across concurrent batches;
   use the recorded session ID. Growing history and stale assumptions can outweigh
   saved exploration. Current Owner calls intentionally start fresh sessions.
5. Try a small repository map before adding a graph service. Add graph retrieval
   only if measured exploration savings exceed tool-schema and result overhead.
   Factual cross-task notes should name supporting files and invalidate on change;
   do not pass the fixer's verdict to an independent verifier as established truth.

Reply wording and commit messages already have deterministic paths; moving them
to a cheaper model would not save an existing model call. Keep the strong final
review, task commits, appended integration repair and final-tree publication gate.

## Assessment of the proposed caching ideas

[Aider's repository map](https://aider.chat/docs/repomap.html) offers compact,
ranked symbol context with a configurable token budget (default 1,000).
[CodeGraph](https://colbymchenry.github.io/codegraph/) offers incremental local
indexing and MCP navigation. Both address repeated discovery; neither establishes
savings on this pipeline without measurement. No graph service was installed.

[Codex non-interactive sessions](https://learn.chatgpt.com/docs/non-interactive-mode)
support explicit session resumption. That is distinct from reusable source facts
and from provider prompt caching.

[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
documents automatic caching and explicit controls for supported API models,
including newer-model breakpoints/lifetimes. The quoted API claim should not be
dismissed as nonexistent, but those controls are not automatically exposed by
our Codex CLI subscription workflow. Cached input still occupies context;
[subscription usage](https://learn.chatgpt.com/docs/pricing) cannot be calculated
from API token prices alone. The observed 89.8% cache rate makes reducing repeated
context and unnecessary attempts more compelling experiments than merely hoping
for more cache hits. This remains a hypothesis until controlled measurement.
