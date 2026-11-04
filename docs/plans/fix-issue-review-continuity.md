# Bound fix-issue repair loops and preserve review decisions

Status: agreed plan, deferred until the recorded test-evidence handoff is implemented.
Issue #15 / PR #36 has merged; implement from updated main after evidence handoff. Do not modify or restart #15 as
part of this work. Model changes on resume are a separate follow-up, issue #37.

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

Run required repository checks before committing. Do not alter the ongoing #15 job.
