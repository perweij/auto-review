# Roadmap

The priority list describes outstanding work, not available features or release
dates. Recently completed work is listed separately for tracking reconciliation.
Current usage is documented in the [manual](manual.md) and
[Emacs reference](../emacs/README.md).

## Priority order (2026-11-10)

The next task is #17. This order favors clear interaction and completing existing
workflows before introducing additional automation or forge support.

| Priority | Work | Tracking and scope |
| --- | --- | --- |
| 1 | Command acknowledgment and human-attention signals | [#17](https://github.com/perweij/auto-review/issues/17): acknowledge recognized commands, explain rejected approvals, and mention the initiator when action is needed; no new configuration. |
| 2 | Complete Owner configuration UI | [#14](https://github.com/perweij/auto-review/issues/14): add editing controls for final-review slots and synthesis, which are already displayed. Shared-engine prerequisite #27 is complete. |
| 3 | Avoid unrelated Git configuration invalidating verification | [#33](https://github.com/perweij/auto-review/issues/33): narrowly tolerate proven-irrelevant metadata while retaining conservative integrity checks. |
| 4 | Compatibility and package validation | Python 3.9 execution, pinned Guix build/installed checks, and packaged LICENSE verification; see below. No matching open issue. |
| 5 | Label-driven monitor | [#18](https://github.com/perweij/auto-review/issues/18): authorized, serialized, resumable unattended operation using current named settings and saved runtime scopes. |
| 6 | Proactive repository reviews | See the detailed specification below. No matching open issue; improvement proposals remain a later extension. |
| 7 | Forge abstraction and GitLab pilot | [#19–#25](https://github.com/perweij/auto-review/issues/19): start with #19, then #20; the read-only #25 pilot can precede the remaining write-path extractions. Promote this work if GitLab becomes an immediate requirement. |
| 8 | Interactive Owner fixes | Requires the live terminal experiment below before implementation. No matching open issue. |

The forge series consists of identity/authentication (#19), review context (#20),
review publication (#21), Owner remote operations (#22), issue eligibility facts
(#23), issue remote writes (#24), and the read-only GitLab pilot (#25).

## Completed work and tracking reconciliation

The following implementations are on remote `main` as of 2026-11-10:

- Accounting improvements: cumulative provider/role usage (`d989f82`) and
  unreliable-token diagnostics (`9a2f9cc`).
- [#37](https://github.com/perweij/auto-review/issues/37): change execution
  settings at stopped boundaries without discarding approved requirements or
  valid completed work (`a2864b1`).
- [#38](https://github.com/perweij/auto-review/issues/38): elaborate on the
  selected Emacs finding, including unsaved editor wording (`b8b3a6d`).
- [#29](https://github.com/perweij/auto-review/issues/29): planner draft,
  independent critique, and planner synthesis with durable stage reuse and
  accounting (`7d5968f`).

These three feature issues were closed as completed on 2026-11-10, with links to
their implementation commits. The remaining 11 open issues are covered above.
All existing PRs were already closed when checked.

GitHub bookkeeping is reconciled: #14 and #19–#25 now reference completed #27 /
PR #28 instead of deleted #13; the forge series acknowledges completed #15 /
PR #36. Issue #17 no longer claims that no issue has been created. Issue #18's
example uses named Codex options (`--codex-1-effort` and `--codex-2-effort`) and
documents the current saved runtime-scope contract.

Files under `docs/plans/` and `docs/evaluations/` retain development history.
Historical instructions and deferred work in those records do not override the
current implementation or this priority list.

## Proactive repository reviews

Add a Repository review toolkit alongside Reviewer and Owner: inspect the default
branch with an optional natural-language focus, triage findings locally, and
explicitly publish selected defects and concrete risks as GitHub issues. Provide
CLI, API, and Emacs entry points using the existing shared backend services.
See the [future-work specification](proactive-review.md).

A later opt-in extension may propose actionable design, maintainability, and
performance improvements. Distinguish those proposals from evidenced defects and
establish their evidence/usefulness criteria before reusing issue publication.
This extension is outside the first proactive-review implementation.

## Interactive owner fixes

Interactive Owner `F` is future work; non-interactive `f` fixes and optional
automatic commits are already implemented. Keep terminal integration dependencies
optional and separate from the existing workflow.

First validate both Claude and Codex in the supported Emacs environment. Evaluate
vterm first, then Eat if needed. Record versions, reproduction steps, and results
for rendering, multiline input, Unicode, scrolling, resizing, streaming output,
and provider dialogs. Verify an Emacs-owned escape binding, interrupt behavior,
process-group cancellation, buffer/Emacs closure, and safe completion before
automatic review starts. If neither integration passes, revise the interactive
design before implementation; do not claim compatibility from mocked tests.

Only after that experiment, implement `F` for individual tasks and sequential
groups using the existing owner worktree, task plans, checkpoints, settings,
outcomes, and review pipeline. Define an explicit finish-and-review handoff and
ensure no interactive writer remains active when automatic review begins.

Exit criteria for that later package: both supported provider CLIs pass the live
terminal acceptance matrix, the user can reliably escape/cancel/return, and
interactive work participates in the same recovery and publication rules.

## Outstanding validation

- Execute the suite on the declared Python 3.9 minimum. Grammar checks on a newer
  interpreter are not a substitute for execution on that version.
- Build and run the installed-package checks with a suitable pinned Guix graph.
  See [package validation](packaging.md#validation); local Python/Emacs checks
  do not establish that the Guix closure builds.
- Confirm that packaged distributions include the root `LICENSE` text.
  The declared license is GPL-3.0-or-later.

Completed specifications and development logs remain available in Git history.

### Forge series implementation sequence

Use #19 → #20 → #25 → #21 → #22 → #23 → #24. The #19 foundation
includes basic capability rejection and saved project/account identity; #20 owns
normalized context and shared read-contract tests. Validate those boundaries
with the read-only GitLab pilot before extending the write interfaces. Retain
existing GitHub storage conventions and legacy receipt interpretation throughout.
