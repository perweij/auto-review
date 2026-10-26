# Auto-review for Magit and Forge

Inspect a saved review in code context, edit comments inline, and move complete
comments between **Unstaged**, **Staged**, **Suppressed**, and **Published**.
Staged is the publication preview. There are no checkboxes or aggregate Markdown
editing steps.

## Install and open

Requires Emacs 29.1+, Magit 4.6+, Transient 0.13+, and Python 3.9+ for the backend
API. Forge is optional for `M-x magit-auto-review`; loading Forge adds an
Auto-review entry to its dispatch and topic menus.

For a source checkout:

```elisp
(add-to-list 'load-path "/path/to/auto-review/emacs")
(setq magit-auto-review-executable "/path/to/auto-review/bin/pr-review")
(require 'magit-auto-review)
```

Open a Git repository or a Forge pull request and run `M-x magit-auto-review`.
Forge dispatch/topic menus use `V`; Magit dispatch uses `C-c r`, leaving its
native `V` (Revert) binding intact. From a repository, Auto-review opens a PR list
using saved local data. Press `g` there to fetch open PRs from GitHub, including
ones with no saved review. Saved closed/merged reviews remain in a separate group.
Select a PR with `RET`, or use `R` to open its setup and `r` to run it. The setup
menu shows the target PR. A sole old saved review is never selected automatically.

If there are several repository remotes, choose which repository to list. Use `o`
in the list (or `C-u M-x magit-auto-review`) to open a known PR number directly.
Opening from a Forge PR still opens that PR directly. Merely opening a buffer
never invokes a model or contacts GitHub; the list shows when it was last checked.
A failed GitHub refresh retains the saved list and reports the error.

For Guix, build or enter an environment containing the paired frontend and
backend from this repository, using your own channels file:

```sh
guix time-machine -C /path/to/channels.scm -- build -f guix.scm
guix time-machine -C /path/to/channels.scm -- shell -f guix.scm emacs
```

The packaged frontend defaults to its matching backend's absolute store path.
You may override `magit-auto-review-executable` for development. No package
installation or dependency updates occur inside Emacs. See
[packaging](../docs/packaging.md) for reusable recipes and installed-package checks.

The backend uses its existing GitHub and model authentication. This package
does not copy tokens into authinfo or change credential storage. Existing `gh`,
Claude, and Codex credentials/environment remain under the caller's control.
Model CLI programs are required only for an explicit model operation.

## PR owner toolkit

Press `M` in the PR list or a toolkit buffer to select **Reviewer** or **Owner**.
The choice is remembered per repository. Existing installations default to
Reviewer. Switching leaves the previous buffer, folds, editor, and running
operation available. `M-x magit-auto-review-owner` opens an owner buffer directly.
Owner mode does not require a reviewer session and has its own keymap and menus.

The owner buffer groups incoming discussions and queued local findings under
**Attention needed**, followed by **Your GitHub comments** when available,
**Fixes**, **Replies**, and **Deferred / History**. Expand an item to read its
complete conversation, code hunk, resolution/outdated status, or review verdict.
An empty Request changes review remains a visible status item without inventing
a code-fix task. New/edited incoming messages return handled or deferred feedback
to attention; the user's own messages remain context without unread alerts.

From Reviewer, press `o` on a finding, a status heading (for example Unstaged), or
a region of sibling finding headings to **Send to Owner for fixing**. The owner
buffer imports those items in their displayed order and focuses the first one;
`f` fixes the item at point. No group region is activated automatically. To fix
a batch, use `f` on an Owner group heading or select sibling headings explicitly.
An Attention needed group includes every item currently in that group, including
previously imported findings. The handoff itself is local and invokes no model.
It uses the saved draft text and preserves the original findings, reviewed commit,
and code/discussion context. Resolve an active inline edit before sending.
Reviewer wording and staging stay unchanged.

Imported items are labeled **Local review**. `G` preserves them; it does not
import unpublished reviewer findings. Repeating `o` on unchanged findings selects
the existing copies. Sending edited wording again saves the prior copy and
invalidates dependent assessments, requiring replanning. Local copies do not
silently follow later reviewer edits or generations. Their commit associations
are retained, but no GitHub reply or resolution target is invented for them.

When you are both author and reviewer, your own published feedback is also
fixable. `G` exposes it under **Your GitHub comments**, without counting it as new
incoming feedback. Use `f` there directly, or `U` to queue it under Attention
needed; `h` and `k` move it to history/deferred. Replies from teammates still
generate attention normally. These feedback items use the usual commit-linked
reply workflow because they already have GitHub destinations.

| Key | Owner action |
| --- | --- |
| `g` / `G` | Reload and check local fix commits / explicitly fetch GitHub |
| `f` | Plan, fix, and review the item, group, or selected sibling headings |
| `V` | Verify selected concerns without a fixer invocation |
| `R f` / `R r` | Choose fix / automatic-review provider, model, and effort |
| `i` / `h` | Acknowledge inspected feedback/context / finish feedback or a verified fix locally |
| `k` / `U` | Defer / restore feedback locally |
| `r` / `A` | Resume the selected batch / explicitly adopt manual changes and reverify |
| `p` / `? S` | Replan unfinished work / skip the selected task and resume |
| `d` / `C-u d` | Saved logical fix/task/batch diff / current partial changes since its checkpoint |
| `m` / `C-u m` | Prepare the selected logical fix or next batch commit / inspect Magit without staging |
| `C-u f` | Fix/review the item or group, then automatically commit and stage replies |
| `C-u r` | Choose automatic or manual completion and resume the selected batch |
| `a` / `C-u a` | Detect exact commits / explicitly associate selected feedback and commits |
| `c` / `e` | Compose a reply / edit an unpublished reply |
| `s` / `u` | Stage / unstage replies, a reply group, or sibling headings |
| `P` / `O` | Preview and explicitly publish staged replies / recover uncertain publication |
| `x` / `X` | Explicitly resolve / reopen a GitHub thread |
| `T` | Inspect changed agent-configuration diff and authorize its exact contents |
| `L` | Progress, or the selected provider attempt's event log |
| `K` | Cancel the owned operation; retain partial work and saved reports |
| `b` / `B` / `M` | Open on GitHub / PR list / toolkit chooser |
| `?` / `? +` | Actions for the current selection / complete owner command reference |

The progress log includes timestamps, PR identities, and the number of selected
feedback items at launch. Finishing a provider process does not mean a fix is
verified: the header/log say **Fix verified** or **Fix paused**, with task counts
and the current blocker. Expanding a paused fix shows that reason without opening
historical reports. `m` explains the pause and opens status only, preserving the
unstaged changes; inspect and resume that batch with `r` for remaining verification.
When the implementation succeeded and only review was incomplete, resuming
unchanged work retries that review and the combined check, without rerunning the
successful fixer. Review can use disposable Git fixtures outside the checkout.
Other operations retain explicit completion and stopped/error messages.

Owner `F` is unbound; interactive provider handoff is deferred. Reviewer `F`
continues to check existing conversations against pushed code.

A group fix snapshots the selected items in displayed order. The planner may
combine overlapping concerns and add prerequisites; independent tasks use display
order as the tie-breaker. Tasks run sequentially in one owner worktree, with a
review after each task and a final review of the combined patch. Inspect task
expectations, per-item outcomes, performed checks, blocking findings, retained
attempts, and commit associations in **Fixes**. Each fix expands to a next step
and short result, plus the included feedback for a group fix. **Verification and task details** and **Attempt history** start
folded; older failed reviews remain in history without obscuring the final result.
`?` offers actions for the selected feedback, fix, or reply, with its next step
above the menu. `+` opens the complete command reference. Settings default from the
customized processing-provider defaults when a session is first opened and then
remain independent of reviewer settings. Model/effort choices come from fresh
provider metadata. Owner verification uses focused checks.

After a pause, inspect the saved patch with `d`, current partial changes with
`C-u d`, and the full worktree/index with `m`. `A` accepts inspected edits for
reverification without moving the original batch baseline. Unstage code before
resuming; source is preserved. `p` replans changed or unfinished feedback against
accumulated edits. Skipped prerequisites block dependent tasks. Changed HEAD
requires manual reconciliation and a new batch. A running buffer can be left
with `q` or by switching toolkits; cancel with `K` before killing it.

For a new `f`, a clean owner worktree that is behind GitHub fast-forwards to the
fetched PR commit before planning. The progress log shows the update, and the new
fix records that commit as its baseline. This lets you continue after pushing
changes from another checkout. A saved batch resumed with `r` keeps its original
baseline. Local changes, unfinished batches at the current HEAD, in-progress Git
operations, or genuine divergence require inspection with `C-u m`; no automatic
stash, rebase, or merge commit is used.

After a successful group fix, expand its batch to see **Logical fix commits (in
order)**. Each reviewed task normally becomes one commit; related findings can
share a task. The batch shows **Fixes committed N/M**, and each commit row names
its findings and either the detected hash or its next step. `m` on the batch
prepares the next uncommitted fix and opens a native Magit commit editor with
that fix's message. `m` on a commit row, task, or feedback targets its logical
fix; feedback uses its latest related batch. Elsewhere `m` uses the latest batch's
next commit. Selecting a later fix explains which earlier commit must come first.
Edit the message if needed, commit with `C-c C-c`, return to Owner and press `g`,
then prepare the next commit. `d` on a commit row shows just that fix's saved patch.
The full group finishes its combined review before commit preparation is enabled.
Staging uses each reviewed snapshot, including earlier versions of files that
later tasks also edited. It does not split work by filename or guess patch ownership.
Combined-review repairs or adopted changes that break those boundaries retain
one combined commit with an explanation. Outcomes satisfied by earlier changes
share a commit rather than creating an empty one.
Later unstaged edits, including edits to the same files, remain unstaged. Existing
staging is accepted only when it contains exact versions from this fix; unrelated
or conflicting staging opens status with an explanation and remains untouched.
Paused, stale, empty, or already committed fixes also open status without preparing
a commit. An existing commit editor keeps its human wording. `C-u m` always opens
status only, including when inspecting partial work or staging manual changes.
When the saved fix already exists as an exact commit in the current branch,
`m` names that commit and opens status without asking for another association or
restaging it. This also works after later commits have advanced HEAD.

For automatic completion, use **`C-u f`** on an item or group. The full group
passes final review before committing begins. It uses the same ordered logical
fixes and explained combined-commit fallback as `m`. Hooks, signing, and identity
follow normal Git configuration. Each commit's actual parent/tree is checked;
unexpected hook changes stop for inspection without resets or lost edits.
There is a 120-second timeout per commit (`PR_REVIEW_COMMIT_TIMEOUT` overrides it).

The batch shows **Auto commit**, progress, and staged reply counts. Its matching
local findings finish automatically. GitHub feedback gets a staged reply with
its own commit link and original thread/timeline destination. **Awaiting push**
means selected for publication but not yet publishable. After pushing, `P` checks
every selected reply and shows the exact preview before asking for confirmation.
A blocked reply stops the whole preview; it is not silently omitted. Changed
context or lost previously confirmed remote commits require inspection. Initial
waiting for a push preserves staged selection.

Ordinary `r` remembers the batch's completion mode. `C-u r` chooses automatic or
manual completion, also allowing an existing verified batch to opt in. A paused
automatic phase retains successful verification and any completed commits.
Recovery detects a commit even if the process stopped before saving its result,
and repairs missing reply drafts without duplicate commits or provider reruns.
Unfinished reply preparation can be resumed even after every commit exists.
An existing human reply, edited wording, explicit unstaging, or uncertain
publication is preserved. Missing destinations and conflicting drafts appear as
reply-preparation issues. Finish/cancel an open Magit commit editor before an
automatic start/resume. `K` cancels; `g` never starts commits or publication.
Once committing has started, replanning/adopting requires reconciliation and a
new batch. Interactive provider `F` remains a separate future feature.

The owner Magit buffer shows **Push PR** with the intended destination. Use `P u`
to push to that upstream after committing. The local owner branch has a unique
name, and the PR may target a fork. New owner branches configure that upstream
without a same-name push-remote; old generated mappings migrate on the next fix
or commit preparation, preserving manual configuration. Missing or ambiguous
remotes require manual upstream configuration. `a` creates staged fixed-in replies
only when exact verified commits have reached the fresh PR head. For a recorded
sequence, each reply links to its own logical fix commit; already committed
prefixes can be associated independently. `C-u a` supports explicit associations
for arbitrary splits or extra changes. Inspect/edit those
replies before `P`; publishing does not resolve threads or change review verdicts.

`a` reports the saved commit associations, or explicitly says when none matched.
Linked hashes appear on the finding and batch headings even when folded. These
are provenance links; local findings still have no GitHub reply destination, and
association does not mark feedback handled or resolve a thread.

After committing, return to Owner and press `g`. It checks reachable local commits
for a unique exact group commit or a recorded sequence of reviewed task trees
with matching sole parents, then
automatically finishes unchanged local findings with verified `fixed` outcomes.
It shows **Committed HASH** and moves both their feedback and fully handled fixes
out of the active sections into **Deferred / History**. GitHub feedback retains
its reply/resolution steps. No GitHub fetch, model, staging, or commit is triggered.
Each confirmed logical fix can finish its local findings while later commits
remain pending. Arbitrary splits/rebases and changed or incomplete verification
require inspection. Existing single commits covering a whole group stay recognized.
An explicit `U` or re-import overrides automatic completion for that finding's
current fix. If a detected commit becomes unreachable, automatically finished
feedback returns to Attention; manual handling stays respected.

`h` still finishes selected feedback or a verified fix manually. Fully handled
fixes move to History and read **Done locally**. No reply is required for local
findings. **Verified** means the automatic review passed; the saved reports and
commit links stay available after completion.
On a batch or task, `h` handles only its verified feedback; skipped and unrelated
items stay as they were. Paused or stale fix results must be inspected before
using this shortcut. Direct feedback triage remains available independently.
GitHub replies, thread resolution, and review verdicts remain separate actions.
Select a particular fix or its feedback before `r`: pressing it on the Fixes
header gives a selection hint and never silently resumes the latest batch.

The owner reply editor uses `C-c C-c` to save locally and `C-c C-k` to cancel.
If saving fails because the session changed, its text remains intact. `C-c C-r`
explicitly refreshes the save revision after you inspect the newer draft.
An edited draft becomes Unstaged. For changed conversation or association context,
inspect it and press `i` on the reply before staging again. `O` reads back uncertain
submissions; only confirmed replies become Published, and an unsent remainder
requires another staging/preview cycle.

See the [owner manual](../docs/manual.md#pr-owner-workflow) for recovery rules
and the [roadmap](../docs/roadmap.md#interactive-owner-fixes) for planned interactive fixes.

## Review flow

| Key | Action |
| --- | --- |
| `TAB`, native section navigation | Fold and navigate comments, code, and conversations |
| `s` / `u` | Stage / unstage the whole comment, status group, or selected sibling headings |
| `e` | Edit the comment body inline; surrounding context is read-only |
| `C-c C-c` / `C-c C-k` | Save the draft locally / cancel the new comment or edits |
| `C-c ?` | Save/Cancel help while typing; `?` remains ordinary punctuation |
| `C-c C-r` | Recover an unsaved/stale edit as a new current draft |
| `C-c C-e` | Return to the active inline editor after scrolling away |
| `c` / `f` | Choose an attachment and compose a new comment / follow-up |
| `o` | Send a finding, status group, or sibling headings to Owner; press `f` there to fix |
| `k` / `r` | Exclude the selected draft from publication (Suppressed) / restore it |
| `a` | Choose code, an existing thread, overall assessment, or automatic placement |
| `RET` | Visit the file at the reviewed revision with the actual range highlighted |
| `d` / `l` | Full PR diff / secondary evidence locations |
| `C` | Open Conversations, including unmatched and outdated unresolved threads |
| `B` | Return to the repository PR list; `g` there discovers PRs on GitHub |
| `M` | Select Reviewer or Owner toolkit |
| `g` | Open the latest completed results locally; history buffers stay pinned |
| `G` | Explicitly refresh GitHub discussion and last-checked source information |
| `R` / `N` | Configure and run/resume / open the current completed review from an older or history view |
| `h` / `y` | Browse history / recover selected saved text into the current review |
| `v` | Choose one overall PR verdict for the next review |
| `P` | Publication Transient: choose verdict with `v`, publish with `p` |
| `b` | Open the published comment at point (or the PR) on GitHub |
| `L` / `?` | Progress and receipt URLs / context-sensitive action menu |
| `K` | Cancel the owned backend operation, retaining saved work |
| `q` | Close a menu or return from a context/history/log window |

`R` opens a populated Transient: change any settings, then `r` to run/resume, or
`q` to cancel. `C-u R` opens the same menu. Saved reviews retain their own settings;
new reviews use `magit-auto-review-reviewers`, `magit-auto-review-claude-model`,
`magit-auto-review-codex-model`, the corresponding `*-effort` variables,
`magit-auto-review-processing-provider`, and `magit-auto-review-synthesis-tests`.
Menu changes do not alter these defaults.

Inside an individual review, `g` reloads that PR's saved results and `R r`
reruns that same PR. To review another PR, use `B`, refresh the list with `g`,
and select it. Switching PR buffers preserves saved history and any inline edit
in the previous buffer.

| Setup key | Setting |
| --- | --- |
| `c` / `x` | Enable or disable Claude / Codex; at least one remains enabled |
| `m` / `M` | Select Claude / Codex model |
| `e` / `E` | Select that model's supported effort |
| `p` | Choose the shared synthesis and discussion provider when both reviewers are enabled |
| `t` | Select focused checks or permit one full-suite attempt |
| `g` | Refresh model choices or retry discovery |
| `r` / `q` | Run/resume / cancel setup |

Model and effort controls open selection-only menus. Choices come from each
installed CLI's metadata using existing credentials; opening setup sends no review
prompt and starts no inference. Models with no advertised effort capabilities are
not offered. Discovery refreshes on opening; cached choices are display-only until
refresh succeeds. Run stays disabled while an enabled provider is loading or has
failed, or a saved model/effort is no longer supported. Choose a listed replacement
explicitly; the tool never silently substitutes a model. `q` cancels discovery too.

With both reviewers enabled, the processing provider uses its selected model and
effort for synthesis, discussion matching, reassessment, and thread adaptation.
With only one enabled, it handles the full pipeline without cross-review synthesis;
the disabled provider requires neither installation nor authentication.

Focused verification permits checks of at most 60 seconds each and five minutes
total, without broad suites or timeout retries. Full permits one suite attempt
with a 30-minute limit plus the focused-check allowance. These are model
instructions, not enforced process timers. The policy applies to synthesis or the
single-provider structured review, is shown in setup, and is saved for resume.
Both independent reviewers always use focused checks, even when Full is selected;
the full-suite permission is reserved for synthesis to avoid repeated suite runs.
Changing processing settings preserves completed generations and their edits.

Sections start collapsed. Status headings contain `severity · filename: finding` headings
directly, with neutral bold text instead of the status heading's accent color.
`TAB` reveals the exact attachment and comment. Moving a comment collapses it
while preserving the folds of other comments and groups.

Severity is the model's impact assessment: Critical, High, Medium, or Low.
Expanded comments show each underlying finding's severity and its separate
blocking/non-blocking assessment. A comment covering several findings shows the
highest known severity; incomplete coverage is labeled “partly unrated.”
Human comments and findings without an identifiable rating show “Unrated.”
Older reviews recover explicit ratings tied to finding IDs in the saved synthesis
report. This is local display metadata: opening or refreshing the view does not
run models, rewrite drafts, change staging, or add labels to posted wording.

New model comments start Unstaged. After staging, point stays on the next
comment in the source section, so consecutive `s` presses work naturally.
Bulk staging skips invalid placements and comments needing individual judgment,
reports the reasons, and moves to the first skipped comment. A finding with no
structured primary location requires a manual placement; prose filenames are
never turned into guessed anchors.

Each draft item contains exactly one comment to publish, under **Your draft
comment** or **Your draft reply**. Human comments have **Draft comment** or
**Draft reply** headings rather than a model severity. The foldable **GitHub
discussion** contains already posted messages for context. If two drafts reply
to the same thread, stage one with `s` and exclude the other with `k` on its own
heading or body. Only that outgoing draft is excluded; its context and history
remain available in Suppressed. `r` restores it. These actions do not alter the
GitHub thread. `?` explains the selected draft, posted discussion, or saved
version; draft actions on posted messages or versions are blocked to avoid
changing the containing draft by mistake.

Code has old and new line numbers. Highlighted lines are the exact attachment,
and your comment appears directly beneath them. Replies follow a foldable full
conversation, including resolved/outdated labels. An outdated conversation uses
its saved original diff hunk. `RET` opens an immutable Git revision, not the
working-tree file. `q` returns from that window, the full diff, or evidence.

`e` puts point in the displayed comment body and enables ordinary text typing.
The body is highlighted and the header starts with **EDITING** and save/cancel
keys. Save, Cancel, and `C-c ?` help also appear immediately above the input.
An empty body shows “Type your comment here…” (or “Type your reply here…”);
start typing at that prompt. The prompt is display-only and is never saved or
published. Context and validation messages remain protected after rejected edits.
`C-c C-e` returns to the body if you scroll away. Single-letter review commands
are suspended until you save or cancel. `c` on a code line selects that attachment;
elsewhere it asks for code, an existing thread, or an overall PR assessment before
opening the editor. The code/thread picker uses `RET` to choose and `q` to cancel.
`f` on a Published comment starts a separate follow-up and asks for its attachment
unless it already identifies a reply thread. Existing drafts use `a` to change
their attachment, including older unplaced drafts.

New comments remain unsaved compositions until they have an attachment and
nonempty text. `C-c C-c` saves them as Unstaged drafts and opens their text under
Unstaged; it does not publish. This includes comments created on code inside a
Staged item: each is a separate draft, with its own code context and attachment.
Saving an existing edit also expands that draft and places point at its text.
`C-c C-k` cancels a new comment without creating an empty item. When editing an
existing draft, cancellation restores its saved text. Publishing remains `P p`
after staging the desired drafts.

Changing text or placement unstages the complete comment and preserves prior
saved wording. Saving unchanged wording preserves staging. Manual placement is
shown with its current code range or discussion in both the expanded item and
the `a` prompt. It is separate from the automatic recommendation and stays selected through context
refreshes. If it becomes invalid, the comment needs correction; it is never
silently redirected. `A`, offered for a draft attached to a discussion, asks the saved processing provider to
consider the full conversation, displays a suggestion, and saves it only after
acceptance. The original remains in saved versions.

Structural redraw is suspended during inline editing. Background progress only
updates the header. A stale save leaves text and undo intact. `C-c C-r` can
recover that unsaved text into the current generation when the previous one has
been replaced.

## Publish what is staged

Expand Staged to inspect the exact wording in code/conversation context. The
heading shows **your** verdict; the model suggestion is separate. `v` (also in
`P`) opens a chooser for Comment, Request changes, or Approve. This is one overall
assessment of the PR, submitted with the staged batch, not a per-comment label.
New drafts default to Comment; Approve and Request changes require your explicit
choice. The model suggestion remains separate, and existing explicit choices are
retained. On your own PR, GitHub permits Comment only, which still
supports inline discussions. The choice belongs to this review, not global
Transient defaults.

`P p` refreshes and validates the source snapshot, complete discussion, account,
and attachments. Changed discussions return dependent comments to
Unstaged. After reading a changed discussion, `i` marks it inspected; this does
not stage, rewrite, or publish the draft. That action is offered only when
inspection is required. Then stage again. The
final confirmation shows counts and the chosen verdict.

One draft becomes one posted comment. New inline comments form a GitHub review;
thread replies go individually to their threads. The optional overall assessment
is explicit text, not a list of findings. At most one overall assessment is
staged per review. Tracking metadata is appended as hidden HTML comments for
recovery. No wording is combined or rewritten during submission.

Partial success moves only confirmed comments to Published. Unsent comments
remain Staged. “Resolve uncertain submissions” checks GitHub for exact markers
before a retry. A known submitted review/verdict is never sent again merely to
finish its replies. Published comments are read-only; create an explicit new
follow-up instead of editing remote text. Confirmed publication produces a
persistent success message and receipt URLs in `L`. Expanded Published items
show their GitHub URLs; `b` opens the selected comment directly. The parent
review URL also appears above the status sections.

The Python backend and terminal CLI always use the same draft ledger and
publisher as Emacs. `pr-review drafts DIRECTORY` opens saved drafts locally
without authentication or model calls. Fresh drafts start Unstaged in both
interfaces; switching interfaces preserves exact edits, staging, and receipts.

## Conversations and follow-up

`C` opens a separate Conversations buffer using the saved snapshot. All unresolved
threads are listed, including outdated ones; resolved threads are available in a
collapsed group. Each conversation contains its complete discussion, saved code
context, participation marker, and any follow-up assessment. `g` loads saved
results and `G` explicitly refreshes GitHub. `q` returns to the preceding view.

| Key | Conversation action |
| --- | --- |
| `c` | Compose an attached reply; save locally with `C-c C-c` or cancel with `C-c C-k` |
| `F` | Check the thread at point or selected sibling thread headings |
| `x` / `X` | Resolve / reopen the thread after fresh permission and context checks |
| `RET` / `b` | Visit saved code / open the conversation on GitHub |

Resolve/Reopen are direct commands, separate from staged publication. If the
source or discussion changed since display, the refreshed view asks you to inspect
it and invoke the command again. An uncertain response is read back; no automatic
retry is sent. Resolution does not change your submitted review verdict.

`F` uses the saved processing provider and focused verification limits at the
latest pinned commit. Its new generation assesses only selected conversations;
it does not scan the whole PR or produce an overall model verdict. Each outcome
includes evidence and limitations. Assessments become stale when their source or
conversation changes. `c` can seed a reply from a fresh saved suggestion, but no
draft, publication, or resolution happens automatically. The publication preview
discloses limited coverage if you explicitly submit a verdict from this generation.

The status summary separates GitHub's review status, your submitted decision,
later Comment activity, and your next publication verdict. GitHub review status
does not establish overall merge eligibility. Unrelated resolution metadata leaves
drafts staged; changes to their discussion dependencies require inspection. New
inline and overall comments retain conservative review-wide discussion dependencies.

Recovery, cancellation, agent-configuration trust, and manual GitHub refresh are
grouped under Advanced in the main help menu, with their existing keys.

## Generations and history

A replacement finishing displays “New review ready” and records completion in
`L`; it leaves the displayed text in place. In the main review buffer, `g` loads
the latest completed results without rerunning analysis. If a replacement is
still running or has failed, `g` keeps the previous completed review available.
An open inline editor prevents refreshing until its text is saved, recovered,
or cancelled. Earlier saved edits, staging, and publication receipts remain in
history; they are not copied into new findings.

In an explicit history buffer, `g` refreshes only that saved generation. `N`
opens the current completed review and leaves read-only history mode after
resolving an active edit. Neither command publishes. To submit the current
staged comments, use `P p`.
The header reports the reviewed commit and when GitHub was last checked; it
does not describe cached information as live.

`h` lists generations, including earlier runs at the same commit, interrupted
attempts, and saved human-edit counts. `RET` opens a read-only contextual view.
Saved text versions are earlier wording for recovery, never additional outgoing
comments. Empty or whitespace-only versions are omitted from the view; their
stored history is retained. Expand a saved version or historical combined document,
then use `y` to recover text into an existing current comment or a new Unstaged
draft. A new recovered draft needs a current attachment before staging.

Old code objects are pinned with Git refs, and conversations and receipts remain
with their generations. No history is automatically pruned. No LLM or heuristic
matches old and new findings to carry human edits forward automatically. Legacy
combined Markdown edits are retained as documents for explicit recovery only.

## Development and validation

```sh
make check-backend              # Python backend and Bats integration contracts
make check-emacs                # Byte compilation and ERT, including real API use
make check-all                  # Both
```

`EMACS` selects the executable. `AUTO_REVIEW_TEST_LOAD_FILE` can load an explicit
dependency graph for sandbox tests; otherwise the profile/site load path applies.
See [contributing](../CONTRIBUTING.md) for dependencies and validation guidance,
[the protocol](../docs/api.md) for the frontend/backend contract, and
[the manual](../docs/manual.md) for the underlying review workflow.
