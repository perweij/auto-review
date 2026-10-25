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

## 2. Python backend migration: implemented; final environment checks pending

The CLI and Emacs UI now share the Python application core and protocol-1 API.
Bash orchestration and jq application logic are retired. Both interfaces use
explicit staging and one draft/publication ledger. Saved reviews, human edits,
receipts, and verified checkpoints remain recoverable.

See the [migration specification and validation record](python-migration.md).
Run the pinned Guix build on a host with Guix before considering the packaging
acceptance gate complete. Also execute the suite on the declared Python 3.9
minimum; this workspace verified its syntax but only has Python 3.12 available.

### PR #3 dogfood findings: fixes implemented and verified

The saved review inspected migration commit `c04bbb4`, before the first-run fix
in `7c73ec4`. Revalidate against the current branch before treating a finding as
open. Local source artifacts are under
`/home/vscode/.local/state/pr-review/github.com/perweij/auto-review/pr-3-5sro0we5/`:
`reviews/synthesis.md` is the complete report, `reviews/synthesis.json` contains
F1–F10, and `publication/drafts.json` contains the seven proposed comments.
Keep these original artifacts intact; track dispositions here.

| Finding | Status and evidence | Next action / acceptance |
| --- | --- | --- |
| F1, High: first-run response shape | Fixed in `7c73ec4`. Emacs expected `sessions[0].session`, but migration returned top-level `session`. Restored protocol 1 response and local rediscovery from an empty buffer. | Covered by first-run/resumed API tests and real-backend ERT tests for both `g` and `N`; no rerun of the user's completed analysis is needed. |
| F2, High: blanket 90-second command timeout | Fixed. Ordinary Git/GitHub commands have no implicit deadline and remain cancellable. Local auth checks use the configured auth deadline; version/help probes retain an explicit 90-second deadline. Model and discovery limits remain separate. | A simulated-clock regression failed before the fix and now passes after 120 seconds of simulated elapsed time; an explicit command deadline still fails with exit 124. |
| F3, Medium: unpromoted replacement reported ready | Fixed. Incomplete discussion or unresolved reassessment records `paused`, reports the current generation plus the pending generation and false readiness, and Emacs announces the pause. First-run `sessions` compatibility is retained. | Two Bats regressions cover initial incomplete discussion, both replacement pause paths, and successful resume without repeating independent reviews. All 63 ERT tests pass, including first-run and replacement pause announcements. |
| F4, High: publication receipts during replacement | Confirmed a narrower defect and fixed. Receipts are preserved in history; treating old posts as discussion for a new analysis is intentional. However, discussion-only rematching recorded the old own-post-excluding fingerprint, then discarded its copied receipts, causing an unchanged resume to pay for another rematch. | The new end-to-end regression failed before the fix and passes afterward: publish a reply plus inline review, change external discussion, rematch, and resume unchanged. Reset candidate publication state and collect the new baseline before matching. Historical receipts remain byte-identical, new counters are zero, and no duplicate posts or extra matching calls occur. |
| F5, Medium: 4 MiB model-event line cap | Fixed. Provider records default to a documented 64 MiB limit, configurable through `PR_REVIEW_MAX_EVENT_BYTES`; pending bytearrays avoid repeated whole-record copying. Stderr is logged without JSON line buffering. | Synthetic 5 MiB success/error results and long stderr pass. A configured smaller limit still rejects oversized complete and partial records. No oversized real provider event was observed. |
| F7, Medium: final draft transaction does not wait | Confirmed a reachable current-review finalization race and fixed, also in cached follow-up adoption. Finalization waits cancellably under the existing PR-before-draft lock order. The report's specific pending-candidate/UI explanation was too broad. | A child process finalizing real saved drafts failed while the parent held a draft transaction. It now waits, succeeds after release, and retains the human text saved during the wait, for both ordinary contributions and follow-up. |
| F10, Low: extensionless entry point skipped by lint | Fixed: `make lint` explicitly invokes `python3 -m py_compile bin/pr-review`. | A temporary copied checkout with deliberately invalid entry-point syntax fails `make lint` at `py_compile`. Normal lint passes; the real source was unchanged by the fault injection. |

All six remaining findings have fixes and focused evidence. Validation on
2026-10-25 passed: all 161 Bats acceptance tests, 94 Python tests, 63 ERT tests,
lint, and `git diff --check`. Production files also parse with Python 3.9 grammar;
the actual minimum-version interpreter and Guix build remain the environment
checks listed above. Tests used synthetic providers/GitHub and temporary
repositories, with no live inference or publication.

The synthesis rejected F6 (claimed reset-publication traceback), F8 (claimed
silent cancellation solely from the exception hierarchy), and F9 (duplicated
checks without a demonstrated inconsistency). They are not established defects;
reopen only with a reachable failing case.

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

## 5. Completed: toolkit selection and the PR owner's toolkit

Implemented the [PR-owner mode specification](pr-owner-mode.md) on
`feat/pr-owner-toolkit`, created from Python-migrated `main` at `f7f0f2e`.

Explicit toolkit selection and an owner inbox work independently of reviewer runs.
Non-interactive `f` fixes use a dedicated owner worktree, with separate
fix/review provider settings, dependency-aware batch planning that can combine
related feedback, per-task and combined verification, and durable checkpoints.
Owner `m` prepares exact verified staging and a suggested native Magit commit
message; `C-u m` opens status only. Human confirmation creates the commit, and
Magit `P u` pushes to the actual PR upstream. Verified commit-linked
replies are staged for explicit publication with recovery for uncertain writes.

The specification retains every delivery milestone, implementation boundaries,
validation evidence, and the subsequent interactive milestone. Terminal, API,
Emacs, and recovery usage are documented. Validation on 2026-10-25 passed all
161 Bats, 141 Python, and 77 ERT tests, lint/byte compilation, and live
planner/fixer/reviewer smoke tests for both Claude and Codex. Interactive `F`
remains separate below.

PR #4 dogfood follow-up: Reviewer `o` now hands selected saved findings to Owner
for explicit `f` fixes, without requiring publication or changing reviewer drafts.
Own GitHub comments are also fixable and appear in a separate section without
self-notifications. Local imports preserve source context and prior versions,
survive refresh, and never invent GitHub reply destinations. See the
[updated owner contract](pr-owner-mode.md) and [Emacs workflow](../emacs/README.md#pr-owner-toolkit).
Follow-up validation on 2026-10-25 passed all 161 Bats, 151 Python, and 83 ERT
tests, lint, and warning-free byte compilation; GitHub and providers were mocked.

The next dogfood adjustment adds explicit timestamped progress conclusions,
corrects generated push-remote configuration, and makes `m` prepare a verified
commit. Later or unrelated edits stay unstaged; conflicting existing staging is
preserved for manual inspection. The owner Magit buffer identifies the actual PR
destination and `P u` action.
Validation passed 161 Bats, 165 Python, and 89 ERT tests, lint/byte compilation,
and a native Magit handoff check in a disposable repository.
Subsequent dogfood clarification: `a` now visibly reports saved associations and
shows linked hashes on folded findings/batches. `m` recognizes an exact fix commit
already in branch history and opens inspection without repeating the changed-HEAD
association advice. This follow-up passed 167 Python and 92 ERT tests plus lint
and warning-free byte compilation.

The completion workflow now labels successful fixes **Verified**, shows their
next step, and folds verification details separately from attempt history.
`h` works from a verified fix/task as well as feedback, moves only its verified
items to History, and labels fully handled fixes **Done locally**. `?` shows
selection-specific actions; `+` retains the complete command reference. Local
findings finish without a GitHub reply, and selected feedback targets its own
related batch for diff/commit/recovery actions.
Validation passed all 100 ERT tests, warning-free byte compilation, and a read-only
render of the saved PR #4 session; no live feedback or worktree was changed.

New fixes now fast-forward a clean owner worktree when the PR has advanced from
another checkout, instead of misreporting this as divergence. Progress names the
old/new commits and the new batch records the updated baseline. Unfinished saved
batches, dirty state, in-progress Git operations, ignored-file collisions, and
true divergence retain their inspection paths. The 94 owner and trust tests
passed, including nine new synchronization cases using temporary repositories.

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

## 7. Optional automatic owner commits

After the human-confirmed commit handoff is established, add an opt-in automatic
commit step to continue the fix pipeline without opening an editor. Reuse exact
verified staging, suggested messages, and commit associations. Define behavior
for hooks/signing failures, changed HEAD/index, cancellation, retry/crash recovery,
and retaining human edits. Committing must not imply pushing, publishing replies,
or resolving threads. This milestone is separate from interactive `F`.
