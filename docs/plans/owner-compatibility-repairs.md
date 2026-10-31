# Prevent incomplete compatibility repairs in Owner

Status: implementation, paired repair replay and planning-first follow-up complete;
both replay acceptance criteria failed (partial improvement). Live PR retest deferred. See
[evaluation](../evaluations/owner-compatibility/README.md).
This is a program-improvement plan, not a plan to manually finish PR #11.

## Evidence and root-cause assessment

Issue #10 / PR #11 now has eleven implementation steps. Step `p2-t1` introduced
canonical reviewer slots and independent processing settings. Its first review
identified four compatibility defects. The bounded repair received those findings,
changed code, and passed its selected tests. A second review then found three
additional settings-precedence/legacy-migration defects; the one-repair budget
correctly stopped execution. No verified fix was committed or published.

Confirmed gaps:

- A task has a broad expected outcome but no recorded set of concrete compatibility
  examples defining what must remain true across its public entry points.
- The repair prompt contains the review report, so this was not lost feedback.
  The implementation and repair tests covered examples of the new representation
  and reported failures, but missed adjacent combinations of explicit defaults,
  single-reviewer overrides and saved legacy configuration.
- Defaults are applied at several entry points; adding a canonical representation
  introduced a second source of truth. The generated code did not consistently
  distinguish absent settings from explicit values or provisional defaults from
  persisted configuration. Those are the immediate code-level causes.
- Verification detected defects and the budget worked. The evidence does not prove
  that another model, more retries, a repository map, or larger prompts would fix
  this class of failure. Do not change those variables in the initial experiment.

The automation-level working hypothesis is insufficient explicit behavioral
contracts and regression coverage during a representation-changing repair.
Prompt changes alone cannot guarantee correct implementations. Evaluate whether
contract/evidence handling improves the outcome on frozen failing cases.

## 1. Preserve a reproducible failing case

Save a sanitized, standalone fixture containing the original task/approved scope,
initial implementation, first review findings, repair result, and second review
findings. Record the checkpoint trees and source provenance. Exclude credentials,
full unrelated history and manual patches from model input.

Useful existing checkpoints in the shared Git repository:

- Before the model repair: `refs/pr-owner/d3b3fc29029eeba2902e6c06/checkpoints/febca8be1f474d259cd65a1514f0aaa3`.
- After the failed repair/review: tree `1a708d7e9153219b890cfc4acaff6c6adc4fdee4`,
  ref `refs/pr-owner/d3b3fc29029eeba2902e6c06/checkpoints/e3bfa0eadc8a4fee9d063fdc237f6e1c`.

The live Owner worktree already contains an assistant-written manual correction.
Archive that delta separately; it is reference evidence, not an automatic success.
Use disposable copies of failing checkpoints for development/replay. Do not reset
live state, erase attempts or increase its repair allowance. Before any later
live retest, reconcile the manual delta explicitly and preserve any intervening
user changes; never silently overwrite the worktree.

Acceptance: independent checks reproduce the three latest defects on the failed
checkpoint and pass on the manually corrected reference. Preserve initial-review
checks too. Keep additional combinations as held-out evaluator tests, unavailable
to the implementation model. Test code must not import the manual correction.

## 2. Make compatibility expectations explicit within existing calls

Extend the Owner task contract with a small structured list of verification cases:
stable case ID, behavioral scenario, expected result, and supporting requirement
or observed baseline behavior. No mandatory extra planning/model call.

For representation changes, ask the planner/fixer to identify relevant input
sources, precedence, absence versus explicit values, old versus new persisted
formats, normalization round trips, and actual public callers. This is general
migration guidance; do not hardcode this repository's settings API or providers
into the workflow. Keep cases task-scoped and bounded; avoid exhaustive products.

Record examples of intended new behavior separately from preserved old behavior.
Baseline behavior is evidence, not an unconditional oracle: intentional changes
must be traceable to the approved plan. Unknown or contradictory requirements
must remain unresolved rather than becoming invented expectations.

For this fixture, the useful case matrix includes:

- Implicit processing for a sole legacy reviewer versus explicit processing.
- Explicit processing in defaults versus explicit request overrides.
- Saved legacy selection versus provisional canonical defaults.
- Persisted canonical settings retaining independent processing.
- CLI/API resolution, review execution and issue-resume callers sharing the same
  contract, rather than merely testing the normalization helper.

Acceptance: cases survive save/resume with their task and plan revision. Legacy
plans remain loadable without retroactively asserting they supplied evidence.
Contract changes invalidate dependent verification; they cannot expand approved
scope or silently rewrite a completed task's proof.

## 3. Require repair evidence for the defect class

Carry the prior findings and task cases into the bounded repair. Require the fixer
to reproduce the reported behavior where feasible, identify the shared invariant,
inspect callers affected by the changed representation, and add focused regressions
covering both the reported example and relevant neighboring cases. Preserve tests
for already-correct behavior. Do not weaken assertions simply to fit new output.

Return per-case evidence with the implementation report: tested, source-traced,
failed or unavailable, with exact commands/results or source references. Test
claims remain model reports, not proof supplied by the backend. Never execute
arbitrary model-provided shell strings automatically in the coordinator.

Keep the independent reviewer responsible for verifying outcomes and challenging
incorrect case expectations. It receives requirements, case IDs and evidence, not
an instruction to accept the fixer's verdict. A successful task review must account
for every required case; missing/failed/unavailable material evidence cannot become
passed verification. Source tracing can suffice where justified; do not require a
new executable test for every case or pretend that passing tests prove coverage.

Keep final whole-issue review and publication gates unchanged. Distinguish real
implementation rejection from unavailable verification and user clarification.
Keep one bounded repair; do not add automatic budget resets or escalation here.

## 4. Implement backward-compatible state and tests

Likely implementation sites: `owner_fixes.py` (contracts, context, validation and
resume), `owner_providers.py` (provider-neutral schemas/prompts), and focused Owner
policy documentation. Extend `issues.py` diagnostics only if the new evidence
needs to be exposed. Both provider adapters must support the same contract.

New outputs use the new contract shape; readers tolerate older saved reports.
Do not invalidate completed legacy work just because evidence fields did not
exist. When a legacy task is actively retried/replanned, establish its new contract
without losing previous attempts, completed steps, checkpoint identity or budgets.
Freeze schema/contract version in the journal where needed. Do not silently modify
a live frozen model configuration.

Regression tests must exercise:

- Old sessions still readable and safely resumable.
- Stable case IDs and complete task/case coverage; unknown/duplicate IDs rejected.
- Missing or contradicted required evidence cannot produce a passed assessment.
- Deliberate behavior changes do not become false legacy-compatibility failures.
- Exact-tree/source and contract changes invalidate prior verification.
- Interrupted repair/resume retains cases, history and consumed budgets.
- Both Claude and Codex schemas/response paths, using mocked providers.
- No publication/commit after incomplete task or final review.

Avoid adding a new global repository index, cache, unbounded preflight test runner,
or model-routing policy as part of this fix.

## 5. Test the improved program against failing checkpoints

Start with one paired first-step replay: old versus improved workflow, on isolated
copies of the same pre-repair checkpoint and recorded findings. Use the same
Terra/high settings, approval scope and existing one-repair allowance. No model
sees the manual patch or held-out tests. Record every planner/fixer/reviewer call,
including failures and retries. Do not automatically expand to all eleven tasks.

Measure external behavioral checks, preservation of first-review fixes, new
regressions, whether independent verification accepts/rejects correctly, manual
interventions, total input/cached/output tokens, model calls and elapsed time.
A failed check is still failure even if the model says passed. Reject improvements
that merely suppress findings or label unfinished implementation successful.

Success means the updated program fixes the known defect family and passes held-out
cases within the existing budget, with independent verification and no manual
source edits. One successful replay is evidence for this case, not a guarantee
or a general model-quality result. If it fails, retain artifacts and revise the
hypothesis before increasing budgets or changing models.

Only after local regressions and replay pass, resume dogfooding on the broken PR
through the supported recovery path. If the exhausted live batch requires explicit
replanning, use the normal journaled action; do not edit counters in session JSON.
Expose a CLI recovery action if the supported Owner action is otherwise inaccessible.
No live PR writes or replay model calls are part of drafting this plan.

### Execution result

Implemented bounded case contracts, repair/review evidence, exact checkpoint
binding, backward-compatible readers and commit/publication/completion guards.
The authorized paired repair replay improved external checks from 8/15 to 12/15;
both independent reviewers correctly rejected their candidate. No live batch was
reset or advanced. The manually corrected reference is archived separately and
is not counted as an automated repair.

The next hypothesis to evaluate is case selection during the existing planning
call, before implementation, organized around public input sources, persisted
formats and precedence. The repair-only treatment largely mirrored the original
four findings and missed legacy saved-state combinations. It is not evidence
that adding more retry attempts or changing models is warranted. The live retest
and any CLI recovery extension remain deferred until the stated acceptance
criterion passes; do not silently broaden this experiment.

The subsequently authorized [planning-first follow-up](../evaluations/owner-compatibility/PLANNED.md)
used the production task loop with eight planned cases and one repair. It passed
15/19 external checks but still failed explicit-default precedence, and independent
review rejected additional saved-setting losses. The live recovery prerequisite
remains unmet. The evidence now points toward specifying consistent value-origin
and precedence semantics rather than merely moving or expanding case generation.
No live state was reset and no additional unrecorded retries were run.

A subsequent tool-free Fable consultation found underspecified precedence rules
and overly broad, conflicting repair instructions. Read-only frontend inspection
confirmed that Emacs always sends the legacy processing default, even without
customization; presence alone is not explicit user intent. See the consultation
section of the follow-up report. Before further trials, ratify the disputed
semantics, encode concrete layered cases, and rescore frozen candidates with zero
model calls. The old oracle's score is not an unconditional recovery prerequisite
until its policy assumptions are settled. No live recovery or manual adoption
has been performed.

The user has now confirmed preservation of saved choices: changing reviewers
alone must not reset saved independent processing, and repeating the same ordered
reviewer list must retain its models and efforts. A separate public-API checker
rescored all four frozen candidates against six concrete scenarios for each
provider: each passed 2/12. The manual reference also remains defective. See
the follow-up report's ratified saved-choice contract and `results/saved-choices/`.
Historical oracle results are retained without being treated as ratified policy.
No new model calls, live plan changes, manual patches or counter resets were made.
The remaining feature fix should follow this contract through a journaled replan;
additional general pipeline machinery is not justified by these results.

At the user's subsequent request, `fix-issue --replan` now exposes the existing
Owner operation as a CLI convenience. It selects one unfinished batch, retains
completed tasks and historical repair counts, and does not imply adoption or
approval. It is not a new recovery subsystem or an automatic retry policy. The
live manual delta still requires reconciliation, and the clarified requirements
must enter the normal issue-plan context before a meaningful live retest.

## Issue #10 follow-up: Emacs processing defaults and independent selection

Status: recorded, not implemented. This belongs to provider independence in
`bin/pr-review` and its Emacs frontend. `bin/fix-issue` is CLI-only: this finding
is not evidence that Emacs supplied configuration to the dogfood failure.
Trace that failure's actual CLI request and saved state separately.

Confirmed in [the setup adapter](../../emacs/magit-auto-review-setup.el):

- `magit-auto-review-processing-provider` defaults to `"codex"`; its docstring
  currently limits its meaning to reviews with both providers enabled.
- `magit-auto-review--default-settings` always includes `processing_provider`,
  without distinguishing untouched defaults from a deliberate customization.
  Consequently, backend code cannot infer explicit independent-processing intent
  merely from the presence of this legacy field.
- `magit-auto-review-setup-submit` overwrites processing with the sole reviewer
  before launching a manual review. That existing behavior must be reconciled
  with issue #10's independently selectable processing role. It is a submit-time
  rewrite, not a reviewer-toggle behavior.

Required follow-up:

1. Define and document how an untouched frontend default differs from an explicit
   processing choice. Check customization, setup-menu selections and saved-session
   loading; do not guess intent from a serialized default value.
2. Adapt the frontend/backend contract so explicit independent processing survives
   submission, while a fresh single-provider setup works without requiring the
   other provider solely because of an inherited default. Preserve deliberate
   cross-provider selections rather than silently rewriting them.
3. Audit manual and automatic launch paths, provider/catalog availability checks,
   saved and pending configurations, and the customization documentation against
   the same agreed policy. The submit-time rewrite is confirmed; the rest are
   audit targets, not assertions that all paths have the same defect.
4. Add focused ERT and backend integration coverage for untouched defaults with
   one provider, an explicit different processing provider, saved canonical
   processing, and equivalent manual/automatic submissions. Keep CLI absence of
   processing flags covered separately; do not impose legacy Emacs serialization
   semantics on `fix-issue`.

Acceptance: independent selections survive the frontend-to-backend round trip;
implicit single-provider use does not acquire an unintended provider dependency;
legacy sessions and frontend defaults follow a documented migration policy.
Record the agreed semantics in issue #10's implementation contract before using
them as expectations in further model evaluations. This local note does not
modify the published issue or approve a revised live plan.

## Follow-up boundaries

If a future failure is solely ordinary generated-code error and the automation
provided the intended context and correctly rejected it, record that result rather
than inventing a program bug. Manual intervention may sometimes be appropriate,
but must be explicitly identified and must not count as autonomous success.
Model escalation and a different repair budget are separate measured experiments.
