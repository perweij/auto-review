# Roadmap

The priority list describes outstanding work, not available features or release
dates. Recently completed work is listed separately for tracking reconciliation.
Current usage is documented in the [manual](manual.md) and
[Emacs reference](../emacs/README.md).

## Priority order (2026-11-16)

The next planning task is GitLab Reviewer support in Emacs. Review, COMMENT
publication, recovery and execution reports already work through the CLI/API;
the frontend still assumes GitHub. Complete that user-facing workflow before
expanding GitLab Owner and issue execution.

| Priority | Work | Tracking and scope |
| --- | --- | --- |
| 1 | GitLab Reviewer in Emacs | Automatically retain forge/project identity when opening an MR or saved session; expose supported review, publication, recovery, checkout and report actions. Keep unsupported actions out of the menus. MR listing can remain a separate increment. No matching open issue. |
| 2 | GitLab Owner | Establish feedback and permission contracts, replies and resolve/reopen, then integrate fixing, verification and publication. Builds on the shared Owner engine and write adapters. No matching open issue. |
| 3 | GitLab issue fixing | Define eligibility and issue-conversation behavior, then support planning, assignment, draft MR creation and ready transitions using the shared workflow. No matching open issue. |
| 4 | Compatibility and package validation | Python 3.9 execution, pinned Guix build/installed checks, and packaged LICENSE verification; see below. No matching open issue. |
| 5 | Label-driven monitor | [#18](https://github.com/perweij/auto-review/issues/18): authorized, serialized, resumable unattended operation using current named settings and saved runtime scopes. |
| 6 | Proactive repository reviews | See the detailed specification below. No matching open issue; improvement proposals remain a later extension. |
| 7 | Interactive Owner fixes | Requires the live terminal experiment below before implementation. No matching open issue. |

The GitLab items are follow-ups beyond the completed #19–#25 forge series.
Current GitLab support covers MR review, COMMENT findings and execution-report
notes; see [capabilities and native validation evidence](gitlab-pilot.md).
Approvals/request-changes, code suggestions, focused follow-up, MR listing and
richer CI coverage are separate extensions, not prerequisites for this first
Emacs increment. Hover tooltips remain deferred pending reliable hosted-renderer
support; expandable report sections provide the detail today.

## Completed work and tracking reconciliation

The following implementations are present in local `main`. This records completed
work without assuming that every local commit has been pushed.

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
- [#17](https://github.com/perweij/auto-review/issues/17): acknowledge issue
  commands and highlight requests for human action (`18f4578`).
- [#14](https://github.com/perweij/auto-review/issues/14): complete Emacs Owner
  configuration with a unified settings draft (`01f7057`).
- [#33](https://github.com/perweij/auto-review/issues/33): preserve Owner
  verification after unrelated Git metadata changes (`c05ca2c`).
- Session checkout access in Emacs and CLI (`a0dfd59`).
- [#19–#25](https://github.com/perweij/auto-review/issues/19): forge identity,
  normalized context, shared write contracts and the initial GitLab review pilot.
  All seven issues are closed; their scope did not include general GitLab writes.
- [#39](https://github.com/perweij/auto-review/issues/39): economical settings
  across issue-workflow roles, merged through PR #41 (`8894708`).
- Execution reports for issue workflows (`3380f7b`), standalone Owner sessions
  (`9ec3872`) and plain reviews (`d67595c`), with recorded stage outcomes and
  causal links (`582c101`). Reports support newly traced sessions; historical
  session reconstruction is outside scope.
- GitLab MR execution-report publication (`7313be5`) and COMMENT finding
  publication with durable receipt recovery (`dcbbfe8`).

GitHub was checked on 2026-11-16: #18 is the only open issue and there are no open
PRs. The completed feature issues above are closed. The remaining local roadmap
items do not yet have matching open issues.

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

### Completed forge series implementation sequence

The delivered order was #19 → #20 → #25 → #21 → #22 → #23 → #24. The foundation
provides capability rejection, saved project/account identity, normalized context,
shared read contracts and adapter-owned write interfaces. GitHub storage and
legacy receipt interpretation remain compatible. GitLab execution-report notes
use a separate capability from COMMENT findings; other GitLab workflows require
new follow-up work.
