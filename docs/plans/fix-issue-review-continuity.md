# Bound fix-issue repair loops and preserve review decisions

Status: implemented on `fix-issue-review-continuity`; pending parent review and merge.
Base: evidence handoff merged to main at `fb3edae`.
Do not modify or restart historical issue #15 as part of this work. Model changes on resume are a separate follow-up, issue #37.

Related: [recorded test-evidence handoff](fix-issue-test-evidence-handoff.md).
Evidence collection and validation are a separate improvement. Review history
should reference those evidence bundles rather than duplicate their contents.

## Summary

Add persistent finding history, one focused adjudication for recurring or
contradictory findings, and a default budget of three repair cycles. Apply to new
fix-issue batches only. Independent reviewers still inspect the full candidate
without seeing previous verdicts. Existing batches retain legacy behavior.

## Review history and adjudication

- Persist a versioned ledger of candidate identities, synthesis artifacts,
  findings, dispositions, evidence and repair cycles.
- Give synthesis the ledger, latest implementation report and changes since the
  previous reviewed candidate. Earlier decisions are evidence, not authority
  overriding current code.
- Extend issue-specific synthesis output with finding lineage: link current
  findings to prior ledger IDs or identify them as new. Require an evidenced
  disposition for every previously open blocker. Assign stable ledger IDs in the
  controller; artifact-local IDs such as F1 remain local.
- Require new evidence and an explanation to reopen a rejected or resolved
  finding. Validate references and coverage. Do not identify recurring concerns
  solely by title or text hashes.
- Trigger adjudication when a closed finding becomes blocking again, or the same
  blocker survives two completed repair cycles. Bundle disputes from that round
  into one focused call.
- Use the configured synthesis provider/model/effort in a fresh isolated session.
  Supply the exact candidate, specification, conflicting decisions, relevant
  reports and repair history. Permit focused inspection and checks.
- Adjudication returns uphold, reject or unresolved for each disputed finding,
  with evidence. It cannot change unrelated findings or acceptance requirements.
  Apply decisions through the normal acceptance validator. Unresolved blockers,
  questions or verification limitations cannot authorize publication.
- Save provenance and artifacts; cache adjudication against candidate, disputed
  history, instructions, validator and effective model settings.

Implement through the issue harness and issue acceptance adapter with a separate
review-history module. Leave Owner and standalone PR review behavior unchanged.

## Budgets and resume

- Add --max-repair-cycles and a matching request field: positive integer, default
  3, persisted per new batch. Keep it outside planning identity.
- Allocate a cycle atomically when acceptance findings are sent for repair. The
  initial implementation/review, output-format corrections and interrupted
  continuations do not each count as cycles. Resume never double-charges a cycle.
- Finish review of an allocated cycle even at the allowance. If blockers remain
  and another cycle would exceed it, return needs_attention, code repair_limit,
  next_action increase_repair_cycles. Increasing the allowance preserves counters,
  approval and completed work, without replanning.
- Unresolved adjudication returns review_conflict with next_action
  inspect_review_conflict. Ordinary unchanged resume makes no further model calls.
- Add --retry-review-adjudication and its request field to authorize one more
  adjudication of the saved conflict. Do not restart independent reviewers or
  grant acceptance. Changed candidates require normal review first.
- Include adjudication in runtime/quota accounting. Retain integrity and existing
  no-progress protections.
- Guard stops retain the local checkpoint and publish one deduplicated concise
  status comment: reason, disputed findings, cycles used/allowed, worktree/artifact
  locations and resume command. Do not push unaccepted code or mark the PR ready.
  Draft checkpoint publication is a separate improvement.

## Compatibility and visibility

- Stamp new batches with a review-control version. Missing version means legacy;
  do not automatically reconstruct old decision histories.
- Freeze ledger input per synthesis attempt so ingesting its result cannot
  invalidate its own cache. Make ingestion and adjudication crash-safe/idempotent.
- Report turns separately from cycles, e.g. implementation turn 8, repair cycle
  3/3. Include stage and stop reason in machine-readable results.
- Document controls, evidence reuse and the local-only handoff limitation.

## Verification

Use mocked providers and disposable repositories to cover:

- Reopening a rejected finding triggers exactly one adjudication; previous
  rejected findings remain visible to synthesis.
- Stable lineage across renamed titles/local IDs; reject invalid references and
  omitted prior blocker dispositions.
- Persistent blockers trigger after two repairs despite candidate tree changes.
- Uphold/reject preserves unrelated findings; unresolved decisions cannot publish.
- Three cycles allowed; fourth needs an increase. Interruptions, caching and crash
  recovery do not double-count or duplicate calls/comments.
- Ordinary unresolved-conflict resume is inert; explicit retry runs only the
  authorized stage.
- Candidate/specification/trust/evidence changes prevent stale proof reuse.
- Independent reviewers receive no ledger; synthesis/adjudication do.
- Legacy behavior and existing issue, engine, commit and publication tests pass.

Run required repository checks before committing. Do not alter saved historical #15 sessions.


## Implementation handoff (2026-11-04)

Implement this plan only, from fb3edae. Model/provider changes on resume (#37),
external evidence imports and draft checkpoint publication remain out of scope.
The existing test-evidence implementation supplies immutable bundle references;
reuse those references and integrity checks rather than copying logs into history.

### Integration boundaries

- Add a focused `review_history.py` module for versioned ledger validation,
  lineage ingestion, repair allocation, disputes and replay-safe state changes.
- Stamp only newly created issue-harness batches in `owner_fixes.execute` with
  review-control version 1 and a default three-cycle allowance. Missing marker
  means legacy, including an existing batch without attempts. Do not reconstruct
  old histories or change Owner/standalone PR behavior.
- `issue_cli` exposes `--max-repair-cycles` (positive int, bool invalid in API)
  and `--retry-review-adjudication`. Omission on resume preserves the saved limit;
  default three applies only at new-batch creation. `issues.Workflow.fix` must
  forward controls through to Owner execution; these are execution controls, not
  named model settings, planning inputs or approval identity.
- `IssueAdapter` alone extends the synthesis schema/context for versioned batches.
  Preserve legacy schema/prompt paths. Independent reviewers still see no ledger
  or implementation history. Synthesis gets the frozen ledger, latest report,
  previous candidate identity and diff since that candidate (bounded artifact,
  with truncation explicit), and evidence bundle references.
- Integrate after validated acceptance synthesis and before allocating another
  implementation turn. Retain raw synthesis artifacts; store adjudication and
  projected outcomes separately. Existing acceptance and publication guards remain
  mandatory, including evidence integrity from the previous feature.

### State and schema contracts

Use a versioned, inspectable ledger owned by the batch (or an atomically referenced
artifact with equivalent durability). Record stable controller IDs, candidate tree
and review commit, raw synthesis artifact/hash, explicit dispositions, provenance,
and repair-cycle IDs. Allocate controller IDs monotonically; local F1-style IDs
remain tied to their own artifact. Never merge concerns by matching titles/hashes.

Define issue-only schema fields for current-finding lineage (current local ID,
prior stable ID or explicit new finding), and previous-finding dispositions
(stable ID, open/resolved/rejected, evidence and reason). Cover every current
finding and every previously open blocker exactly once; reject unknown IDs,
duplicates, omitted blockers and contradictory lineage/dispositions. Make rejected
findings available to later synthesis, not only open findings. Preserve the
original finding and per-candidate history when its title changes. Reopening a
closed finding requires new evidence/reason plus adjudication, not automatic
acceptance of the new claim. A rejected concern should not disappear from history.

Freeze the ledger revision before a synthesis attempt. The identity uses that
frozen input, not the post-ingestion ledger. Ingest a given validated artifact only
once, keyed by candidate plus artifact/content identity. A crash after ledger save
but before phase save must not rerun synthesis or assign new finding IDs. Preserve
normal invalidation when candidate, specification, trust, evidence, instructions or
relevant effective settings change. Do not make history invalidation rerun otherwise
valid independent reviewer reports.

### Repair cycle transitions

Initial implementation/review is cycle 0. Allocating a repair persists a unique
cycle ID, originating synthesis, selected blockers, consumed allowance and the
transition to implementation together, before invoking a provider. Interruptions,
format corrections and continuations within that cycle never allocate another.
A cycle completes when its candidate receives a completed acceptance decision;
incomplete verification or interrupted reviews do not count as completed repairs.
Track persistence of a stable blocker across completed repairs, even if candidate
bytes/title/local finding IDs change. Trigger one bundled adjudication after the
same blocker survives two completed repairs, or whenever a closed blocker reopens.

At the allowance, finish any already allocated implementation/review/adjudication.
If another repair is needed, save a repair-limit stop before allocating or calling
anything. Raising the limit resumes from that decision without repeating valid
review or planning. Do not reset consumed counters. Define/document behavior for
explicitly lowering an allowance; never permit it to erase consumed cycles.
Retain existing no-progress and integrity checks.

### Adjudication and stopped resumes

One fresh isolated call per disputed round, using the configured synthesis
provider/model/effort and focused inspection policy. Reuse existing subprocess,
resource accounting and disposable checkout primitives. Preserve the current
candidate before/after checks and saved output/log artifacts. No nested review
pipeline or autonomous repair call from adjudication.

Its schema names each disputed stable ID exactly once with uphold/reject/unresolved,
reason and evidence. No decision on an unrelated ID is permitted. Apply decisions
only to disputed blocking status, preserving unrelated findings, outcome coverage,
questions, specification and verification blockers. Rejection cannot manufacture
missing acceptance outcomes or turn incomplete verification into a pass. Validate
any resulting acceptance through the existing validator; if complete acceptance
cannot be established, stop with explicit unresolved details rather than publishing.

Cache against exact candidate/specification/trust/evidence identity, disputed
history snapshot, instructions/schema/validator and effective model choice. Save
request identity before invocation; save validated response before applying it.
Replay after a crash must not call again or apply a disposition twice.

Unresolved adjudication persists `review_conflict` and an inert resume marker.
An unchanged ordinary resume must make zero model calls, including auth probes:
place the stopped-resume check before model preflight, while still validating the
saved candidate/specification/trust/evidence. `--retry-review-adjudication` consumes
one durable retry authorization and runs only that pending adjudication, not the
independent slots or synthesis. Retrying an already consumed request after a crash
resumes its saved stage; it must not allocate another call. A later explicit CLI
invocation can request another retry if the conflict still remains. Reject the
retry flag when no retryable saved conflict exists. Changed candidate/context
requires normal review before any fresh adjudication or publication.

### Stops, visibility and tests

Extend the existing error/status path in `issues.dispatch` for `repair_limit` and
`review_conflict`. Use the existing write journal for one concise deduplicated
comment per stop identity. Include reason, stable disputed IDs, cycles used/allowed,
retained local worktree/artifact paths and an exact resume command. State clearly
that code remains local and unaccepted. Include stage/cycle/stop fields in JSON
results and distinguish implementation turn from repair cycle in progress text.
No GitHub writes during development; exercise publication with mocks.

Use fixture-driven mocked provider tests and actual workflow orchestration for:

1. Initial pass; one normal repair; three allowed repairs and blocked fourth;
   allowance increase resumes without planning or repeating completed reviews.
2. Renamed/local-ID-changed finding lineage, unknown/duplicate IDs, omitted blocker
   dispositions, rejected history retained, and evidence-required reopening.
3. Same stable blocker after two completed repairs across changing trees triggers
   one bundled call; incomplete verification does not count as a completed repair.
4. Uphold/reject affect only disputed findings; unresolved/questions/evidence gaps
   never publish; inability to establish full acceptance remains a stop.
5. Ordinary conflict resume makes no implementation/reviewer/synthesis/adjudication
   or authentication call; explicit retry calls only adjudication once.
6. Crash at allocation, ledger ingestion, synthesis completion, adjudication response,
   disposition application and comment publication; no duplicate IDs/cycles/calls.
7. Changed candidate/specification/trust/evidence invalidates stale reuse correctly;
   evidence integrity and source/publication protections remain effective.
8. CLI and API controls, no planning identity change, legacy batches unchanged,
   Owner/standalone PR contexts untouched, and concise deduplicated stop comments.

Run focused tests during development, then required `make check-backend` and
`make check-emacs`. Preserve logs; report actual counts and limitations. If broader
checks find outdated mocked response fixtures, update only fixtures exercising the
new schema and rerun affected checks/full Python as needed, not all passed Bats.
Use no live paid model evaluation. Do not push or merge the implementation branch;
parent will review the diff and final validation before deciding next steps.


## Implementation result (2026-11-04)

New issue batches carry `review_control_version: 1`, a three-cycle default and a
batch-owned ledger. Missing markers remain legacy, including untouched batches;
explicit new review controls on legacy batches are rejected. Limits are execution
controls and do not enter planning/approval identity. Lowering an allowance keeps
all consumed cycles and finishes allocated work before stopping new allocations.

Synthesis receives a frozen ledger revision, the full latest implementation
report, the previous candidate and a bounded 64 KiB diff with explicit truncation.
Stable IDs come only from controller allocation and explicit validated lineage.
Incomplete verification retains its allocated cycle and creates a new frozen
input on the next synthesis attempt. Completed synthesis ingestion, repair
allocation and adjudication request/response/application replay idempotently.
Independent reviewers receive no implementation history or ledger.

One fresh focused adjudication bundles a round's reopened/persistent blockers.
Persistence counts only completed repairs that targeted the stable blocker; an
uphold resets persistence, requiring two further completed repairs. Reopening a
closed concern still triggers immediately. Existing no-progress protection stops
after review establishes a third unchanged rejection, before adjudication;
new evidence/reports still receive review for unchanged source. Raw
synthesis remains intact; projected decisions preserve unrelated findings,
questions, outcomes and verification blockers and pass the existing acceptance
validator before any proof can be written.

Unchanged conflicts stop before model discovery/authentication at both Workflow
and Owner entry points while checking current source/specification/trust/evidence.
Explicit retries use durable authorization and run only adjudication. A resolved
retry with remaining blockers returns `needs_attention`,
`stop_reason: adjudication_complete`, `next_action: resume_repair`; an ordinary
resume uses the saved projected decision before allocating further repair. A
fully accepted retry continues through existing commit/publication guards.
Frozen adjudication request/input hashes preserve original context bytes across
response/application crashes. Successful guarded raw subprocess responses can
also recover before validation is saved; failed candidate-checkout stages are
never reused. Stops use the existing write journal, retain local
unaccepted work and show stable IDs, cycles, paths and an exact resume command.

Validation (mocked providers and disposable repositories only):

- Focused continuity/real-engine suite: 36 passed; `/tmp/continuity-focused-final.log`.
- Existing issue/workflow suite: 190 passed; `/tmp/continuity-workflow-existing.log`.
- Final lint: passed; `/tmp/continuity-lint-final.log`.
- Initial `make check-backend`: lint and 195 Bats passed; Python ran 1000 tests with two failures from the subsequently corrected pre-review no-progress ordering and one specification-fixture error from earlier integrity validation. Original log: `/tmp/continuity-check-backend.log`.
- The three affected issue/continuation/specification modules passed all 62 tests after correction; `/tmp/continuity-affected-fixtures.log`.
- Final full Python discovery after all source/fixture fixes: 1003 passed in 334.191s; `/tmp/continuity-python-final.log`. The superseded partial run is retained at `/tmp/continuity-python-before-fixture.log`.
- `make check-emacs`: 162 passed; `/tmp/continuity-check-emacs.log`.

No live paid model evaluation, GitHub writes, push or merge was performed. Saved
historical issue #15 sessions were not touched. Model/provider changes on resume
(#37), external evidence import and draft checkpoint publication remain out of
scope. The local worktree/session remains the only checkpoint handoff.
