# Planned work

## Completed: Add an Advanced section to the Emacs help menu

Implemented with reviewer follow-up. The main help menu groups `T`, `G`, `K`, and
`y` under Advanced and preserves their bindings and existing contextual visibility.
`T` remains available so a saved trust request can be inspected after a failure.

The `?` menu mixes routine review actions with commands used only for recovery or exceptional situations. Add an **Advanced** section to `magit-auto-review-dispatch` so the usual review flow is easier to scan. Keep the existing keys.

Proposed moves:

| Key | Operation | Why it belongs in Advanced |
| --- | --- | --- |
| `T` | Confirm agent configuration | Needed only when a review stops because the PR changes files loaded by the agents. Clarify the label and show it only while confirmation is pending, if practical. |
| `G` | Refresh GitHub context | Manual refresh for changed remote discussion; ordinary result refresh uses `g`. |
| `K` | Cancel model operation | Relevant only while a model operation is running. |
| `y` | Recover saved wording | Recovery action for a saved earlier draft. |

Leave running a review (`R`), inspecting results, staging comments, and publishing (`P`) in their current sections. Keep context-dependent visibility for the moved commands. Verify that `?` shows the new grouping and that each key still invokes the same command.

## 2. Consolidate the backend in Python

Implement the [Python backend migration specification](python-migration.md).
Keep both the CLI and Emacs UI, with one Python application core and a stable
version-1 JSON API. Replace Bash orchestration and jq application logic in
stages, consolidate draft/publication behavior, and preserve saved reviews,
human edits, receipts, cancellation, and verified checkpoint reuse.

Complete this migration before substantially extending the backend for proactive
repository reviews. The specification defines compatibility requirements,
delivery stages, validation gates, and packaging changes, including the
terminal's transition to the shared Unstaged/Staged draft workflow.

## 3. Add proactive repository reviews

Implement the [proactive repository review specification](proactive-review.md):
review the default branch with an optional natural-language focus, triage findings
locally, and publish selected defects and concrete risks as individual GitHub
issues. Include CLI, JSON API, and a dedicated Emacs workflow designed to become
the future Repository review toolkit major mode. The broader mode system and PR
owner's toolkit are separate work, outside this specification.

## 4. Include improvement proposals in proactive reviews

After proactive repository reviews are available, plan an opt-in extension for
actionable design, maintainability, and performance improvements even when there
is no evidenced defect. Distinguish these proposals from defects and concrete
risks, establish their evidence and usefulness criteria, and reuse the local
triage and issue-publication workflow. This extension is intentionally excluded
from the [first-version specification](proactive-review.md).

## 5. Add toolkit selection and the PR owner's toolkit

Implement the [PR-owner mode specification](pr-owner-mode.md) after the earlier
scheduled work. This is planned, not started; create its feature branch only
when implementation begins and reconcile the plan with the completed Python
backend migration and other intervening changes.

Add explicit toolkit selection and an owner inbox independent of reviewer runs.
Support non-interactive `f` fixes in a dedicated owner worktree, with separate
fix/review provider settings, dependency-aware batch planning that can combine
related feedback, and durable checkpoints. Use native Magit for human staging,
committing, and pushing, then prepare verified commit-linked replies for explicit
publication.

The specification documents all delivery milestones: preparation; toolkit shell
and inbox; individual fixes and recovery; dependency-aware batches; commit-linked
replies; and integration/documentation. The first increments are reviewable
separately, but group fixes and per-task review remain part of this package.

## 6. Add interactive PR-owner fixes (`F`)

After the non-interactive owner workflow, implement the
[later interactive milestone](pr-owner-mode.md#later-milestone-interactive-fixes)
as a separate work package. First test Claude and Codex in a supported Emacs
terminal, evaluating vterm before Eat if needed. Validate rendering, multiline
input, resizing, scrolling, escape keys, process ownership, cancellation, and
safe completion before automatic review.

Choose the terminal integration only after that experiment. Then add individual
and sequential-group `F` sessions using the existing owner worktree, plans,
checkpoints, settings, and review flow. Terminal dependencies and this experiment
are excluded from the initial owner-mode package.
