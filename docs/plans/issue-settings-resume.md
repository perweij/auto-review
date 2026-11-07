# Change execution settings while resuming an issue (#37)

Status: implemented and validated. Base: `9a2f9cc`.

## User contract

Edit the existing settings file or pass existing model/provider/effort flags,
then resume the normal issue command. No transition approval, replacement plan,
or session-management question is introduced. Omitted choices keep their saved
values. Print changed effective choices and the automatic continuation decision.

Changes take effect while holding the existing issue and Owner execution locks,
after validation and before another model call. Record before/after settings and
application progress durably so an interrupted transition resumes idempotently.
Retain active runtime, quota observations, repair cycles and publication receipts.
Existing integrity, approval, specification and exact-candidate guards still run.

## Reuse and handoff

- Planning validity describes requirements, discussion, recorded base and planning
  policy, rather than the models selected for future calls. Migrate a legacy
  identity only after verifying its original inputs. Record the configuration of
  the assessment separately; never relabel an old assessment.
- Completed review stages remain evidence under their actual original settings
  when their substantive inputs and artifacts still match. New settings govern
  missing, failed or invalid stages. A source, specification, evidence, policy or
  reviewer-input change still invalidates the affected evidence.
- A same-provider model/effort change continues the explicit implementation
  session. A provider change starts a fresh session from the approved document,
  current worktree/checkpoint, feedback and a bounded continuation summary. Do not
  reuse an older conversation when switching back to a previous provider.
- Started legacy task pipelines remain unsupported; do not synthesize missing
  provenance or weaken their recovery guards.
- Settings for future work do not invalidate a completed commit or publication.
  They never alter the settings recorded on completed attempts.
  Cached completion applies future Owner settings and records the transition as
  applied; subsequent Owner edits remain authoritative unless explicitly overridden.

## Explicit regeneration

Add optional repeatable `--rerun-review STAGE` (`reviewer-1`, `reviewer-2`,
`synthesis`; API `rerun_review` list). It is unnecessary for ordinary settings
changes. Rerunning a reviewer also reruns synthesis, retaining peer evidence and
archiving prior artifacts through the shared engine. Persist rerun identities
before execution so crash recovery does not allocate another rerun. Bind it to
the selected batch, approved plan and candidate; changes of scope automatically
supersede that request and use normal verification. A different explicit stage
selection supersedes the pending intent automatically while preserving allocated
work; ordinary resume retains the saved selection. Refuse a rerun while repair or
adjudication is required, after completed execution (including no-change
verification), or after the batch has entered committing/publication; do not change accepted
commit provenance or automatically create another implementation batch.

For #29, model changes select future planning execution. Completed approved
assessments remain valid until requirements change or the user explicitly asks
for regeneration. Stage-specific planner regeneration can extend this policy
without silently spending on a new assessment during ordinary resumption.

## Verification

Exercise stopped implementation and review, same-provider and cross-provider
changes, omitted settings, invalid choices, unchanged approved requirements,
completed stage reuse, explicit regeneration, interrupted transition recovery,
legacy identity migration, real provider argument construction and retained
resource/provenance records. Use scripted providers and disposable repositories.
Run backend checks and update CLI/API/manual documentation before completion.

## Implementation and validation

The workflow separates semantic assessment identity from future execution choices,
verifies legacy identities before upgrading them, and records the configuration
that actually produced an assessment. Workflow transition intent survives restart;
batch settings change atomically under the Owner execution lock. Later requests
can supersede unapplied choices without losing retained source or resource use.

Implementation conversations use an explicit active-session identity. Model/effort
changes reuse that session; provider/specification changes create a fresh generation
with a bounded summary and references to complete retained reports. Completed
review stages are checked under their original choices and provenance. Invalid
or missing evidence runs under the new choices. Saved adjudication responses keep
their original attribution even when requested future choices differ.

Provider metadata is not required to save future choices while waiting or to reuse
a completed result. Actual model calls still validate choices before execution.
Regeneration is optional, durable, and scoped to the original work; changed scope
automatically uses normal verification. CLI and API documentation are updated.

The Fable follow-up protects saved repair/conflict decisions and durably ingested
blocking verdicts from regeneration, including an allocated repair interrupted
before implementation. Completed no-change verification is also a terminal
boundary. A completed repair can still regenerate unfinished verification when
the source tree is unchanged. New explicit stage selections supersede pending
intent; applied Owner receipts recover before interpreting stale workflow intent.
Cached settings saves update the Owner and finish their transition, preserving
completed batches and respecting later Owner edits. The narrower raw-adjudication
recovery optimization across a model change remains deferred.

Validation used scripted providers and disposable Git repositories:

- Lint passed; `/tmp/issue-settings-fable-python-final.log`.
- All 197 Bats integration tests passed; `/tmp/issue-settings-fable-shell.log`.
- Final Python discovery passed all 1,082 tests;
  `/tmp/issue-settings-fable-python-final.log`.
- All 48 focused settings/continuity recovery tests passed;
  `/tmp/issue-settings-fable-recovery-final.log`.
- Focused checks cover runtime-limit continuation, approval preservation, provider
  handoff, partial-review reuse, explicit regeneration, invalid choices, legacy
  settings, and interrupted/superseded transitions.

No live paid provider validation, production issue execution, GitHub publication,
or changes to historical saved jobs were performed.
