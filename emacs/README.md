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

For the adjacent stable-emacs checkout, use its `manifest-auto-review.scm`
overlay. The frontend and backend are copied into the Guix store from this
checkout, and Magit/Forge/Transient still come from stable-emacs's one Guix pin:

```sh
cd ../stable-emacs
guix time-machine -C channels.scm -- shell -m manifest-auto-review.scm -- \
  emacs --init-directory="$PWD/emacs.d"
```

Set `AUTO_REVIEW_SOURCE` to an absolute checkout path when it is not adjacent.
This is a development source snapshot, not a fabricated release commit pin.
The normal stable-emacs manifest stays usable without the overlay. Its init
loads auto-review when the package is in the closure. Build the package alone
with `guix time-machine -C ../stable-emacs/channels.scm -- build -f guix.scm`.
No package installation or dependency updates occur inside Emacs.

The backend uses its existing GitHub and model authentication. This package
does not copy tokens into authinfo or change credential storage. Existing `gh`,
Claude, and Codex credentials/environment remain under the caller's control.
Model CLI programs are required only for an explicit model operation.

## Review flow

| Key | Action |
| --- | --- |
| `TAB`, native section navigation | Fold and navigate comments, code, and conversations |
| `s` / `u` | Stage / unstage the whole comment, status group, or selected sibling headings |
| `e` | Edit the comment body inline; surrounding context is read-only |
| `C-c C-c` / `C-c C-k` | Save / cancel the inline edit |
| `C-c C-r` | Recover an unsaved/stale edit as a new current draft |
| `C-c C-e` | Return to the active inline editor after scrolling away |
| `c` / `f` | Create a comment at the code cursor / a follow-up draft to a published comment |
| `k` / `r` | Suppress / restore a comment; suppression is reversible |
| `a` | Choose code, an existing thread, overall assessment, or automatic placement |
| `RET` | Visit the file at the reviewed revision with the actual range highlighted |
| `d` / `l` | Full PR diff / secondary evidence locations |
| `C` | Open Conversations, including unmatched and outdated unresolved threads |
| `B` | Return to the repository PR list; `g` there discovers PRs on GitHub |
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

Code has old and new line numbers. Highlighted lines are the exact attachment,
and your comment appears directly beneath them. Replies follow a foldable full
conversation, including resolved/outdated labels. An outdated conversation uses
its saved original diff hunk. `RET` opens an immutable Git revision, not the
working-tree file. `q` returns from that window, the full diff, or evidence.

`e` puts point in the displayed comment body and enables ordinary text typing.
The body is highlighted and the header starts with **EDITING** and save/cancel
keys. `C-c C-e` returns there if you scroll away. Single-letter review commands
are suspended until you save or cancel. `c` on a code line creates a new comment
attached to that line; elsewhere it creates an unplaced draft. `f` on a Published
comment creates a separate follow-up draft: save its wording, then use `a` to
choose its discussion or code attachment.

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

After drafts have been saved through the API, the terminal CLI uses the same
draft ledger through a terminal adapter. Switching interfaces cannot publish
an obsolete generated aggregate body.

## Conversations and follow-up

`C` opens a separate Conversations buffer using the saved snapshot. All unresolved
threads are listed, including outdated ones; resolved threads are available in a
collapsed group. Each conversation contains its complete discussion, saved code
context, participation marker, and any follow-up assessment. `g` loads saved
results and `G` explicitly refreshes GitHub. `q` returns to the preceding view.

| Key | Conversation action |
| --- | --- |
| `c` | Create an attached Unstaged reply and edit it in the drafts buffer |
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
Expand saved text versions under a comment or historical combined documents,
then use `y` to recover text into an existing current comment or a new Unstaged
draft. A new recovered draft needs a current attachment before staging.

Old code objects are pinned with Git refs, and conversations and receipts remain
with their generations. No history is automatically pruned. No LLM or heuristic
matches old and new findings to carry human edits forward automatically. Legacy
combined Markdown edits are retained as documents for explicit recovery only.

## Development and validation

```sh
make check-backend              # Shell/Bats and Python API contracts
make check-emacs                # Byte compilation and ERT, including real API use
make check-all                  # Both
```

`EMACS` selects the executable. `AUTO_REVIEW_TEST_LOAD_FILE` can load an explicit
dependency graph for sandbox tests; otherwise the profile/site load path applies.
The package has been exercised with Emacs 30.2 and the stable-emacs versions
Magit 4.6.0, Forge 0.6.7, and Transient 0.13.2. That runtime check is distinct from
building a Guix closure. The nested development container cannot perform the
closure build; validate it on the normal supported host/build container.

See [the protocol](../docs/api.md) for the frontend/backend contract and
[the specification](../plan.md) for the underlying review pipeline.
