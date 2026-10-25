# Planned work

## 1. Add an Advanced section to the Emacs help menu

The `?` menu mixes routine review actions with commands used only for recovery or exceptional situations. Add an **Advanced** section to `magit-auto-review-dispatch` so the usual review flow is easier to scan. Keep the existing keys.

Proposed moves:

| Key | Operation | Why it belongs in Advanced |
| --- | --- | --- |
| `T` | Confirm agent configuration | Needed only when a review stops because the PR changes files loaded by the agents. Clarify the label and show it only while confirmation is pending, if practical. |
| `G` | Refresh GitHub context | Manual refresh for changed remote discussion; ordinary result refresh uses `g`. |
| `K` | Cancel model operation | Relevant only while a model operation is running. |
| `y` | Recover saved wording | Recovery action for a saved earlier draft. |

Leave running a review (`R`), inspecting results, staging comments, and publishing (`P`) in their current sections. Keep context-dependent visibility for the moved commands. Verify that `?` shows the new grouping and that each key still invokes the same command.

## 2. Add proactive repository reviews

Implement the [proactive repository review specification](proactive-review.md):
review the default branch with an optional natural-language focus, triage findings
locally, and publish selected defects and concrete risks as individual GitHub
issues. Include CLI, JSON API, and a dedicated Emacs workflow designed to become
the future Repository review toolkit major mode. The broader mode system and PR
owner's toolkit are separate work, outside this specification.

## 3. Include improvement proposals in proactive reviews

After proactive repository reviews are available, plan an opt-in extension for
actionable design, maintainability, and performance improvements even when there
is no evidenced defect. Distinguish these proposals from defects and concrete
risks, establish their evidence and usefulness criteria, and reuse the local
triage and issue-publication workflow. This extension is intentionally excluded
from the [first-version specification](proactive-review.md).
