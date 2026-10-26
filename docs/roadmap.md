# Roadmap

These items describe outstanding work, not available features or release dates.
Current usage is documented in the [manual](manual.md) and
[Emacs reference](../emacs/README.md).

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
