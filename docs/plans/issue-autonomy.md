# Issue execution: harness delegation

## Architecture

The approved specification is the unit of implementation. A persistent provider
CLI session owns decomposition, coding, testing and debugging. The thin issue
coordinator owns approval, worktree identity, resource controls, GitHub messages
and safe publication. This supersedes the controller/requirement-ledger/recovery-
strategy design: those added mechanisms duplicated the harness and caused frequent
handoffs before a complete candidate existed.

Implementation resumes by explicit provider session ID. Same-provider model and
effort changes continue that session; provider or approved-specification changes
select a fresh session with saved work and continuation context. See the
[settings-resume contract](issue-settings-resume.md). PR feedback under the
same specification returns to the existing implementation session. Review runs in
a fresh read-only session only after implementation reports ready. Independent
review findings go back to implementation. Neither reports nor review automatically
trigger backend execution of test commands.

Existing Owner records and commit/publication transactions are shared primitives.
Harness resumption branches before legacy task transitions. A commit-boundary
record is created only after final review; no synthetic unfinished task controls
the harness.
Interactive Owner planning and focused verification stay separate. Unfinished sessions from the retired task pipeline are rejected without modifying
their worktrees or artifacts. New candidates use one
verified commit rather than synthetic task partitions.

## Acceptance scenarios

- Complete candidate: implementation, independent review, verified commit/push,
  ready notification. No separate PR-review pipeline follows the push.
- Review interruption: resume review only; do not restart implementation.
- Partial turn: resume the same implementation session, without intermediate review.
- Review defect: send actionable feedback to that implementation session; review
  the repaired candidate independently.
- Report format error: one report correction, without inventing passing evidence.
- Product question: publish on GitHub; preserve work/session while awaiting answers.
- Provider interruption: retain exact session and post-interruption source identity;
  resume only recorded changes. External edits require adoption.
- Protected Git changes or read-only review edits: block publication.
- Retired paused run: reject with `unsupported_session`, preserving saved work.
- Publication: exact reviewed final tree required, including after integration changes; uncertain remote writes use existing read-back recovery.
- Runtime/quota guards: preserve cumulative accounting across invocations, with
  explicit runtime fallback when subscription telemetry is unavailable.
- Logs: human progress and active elapsed time on stderr; one JSON result on stdout.

## Validation and live rollout

Deterministic tests mock the CLI transport and use real temporary Git repositories.
They must not launch paid model jobs. Run backend and frontend regression suites.
Do not manually edit PR #11 to make the test pass. Its earlier runtime-interrupted
partial edits may predate recorded interruption checkpoints: inspect/adopt those
once if necessary. Increase the cumulative runtime allowance explicitly for a new
live run; prior consumed time is not refunded.

## Cleanup after independent review

Removed issue recovery credits, retry authorization, automatic task-plan repair,
old task-format upgrades, their provider schema/prompt fields, and the issue
`--replan` control. The temporary legacy task/question migration module has also
been removed after the development sessions completed.
Interactive Owner's explicit planning, bounded task repair and verification
contracts remain separate. Runtime/quota controls remain explicit; no fixed
implementation-turn budget was added.

Live transport smoke checks on 2026-11-01 passed with Codex `gpt-6.1-sol/high` and
Claude `opus/high`: a fresh turn and an explicit-ID resume both returned structured
reports, kept the same session ID, and recalled a marker absent from the resumed
prompt. These ran in disposable repositories, without source or GitHub changes.
