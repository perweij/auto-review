# pr-review

A local GitHub PR review tool with a Magit/Forge interface. Run Claude, Codex,
or both against the same Git snapshot. With both enabled, a selected provider
challenges and consolidates the independent reports. That provider also compares
findings with existing discussions and drafts useful contributions. GitHub
publication requires human selection and explicit confirmation.

## Install

Keep this directory intact and put `bin/pr-review` on your PATH, for example:

```bash
mkdir -p ~/.local/bin
ln -s "$(pwd)/bin/pr-review" ~/.local/bin/pr-review
```

Requires Linux, Bash 4.4+, Python 3.9+, Git, `gh`, `jq`, `flock`, `timeout`,
and standard GNU utilities, plus the model CLIs for enabled reviewers.
The CLI checks capabilities through help output before
creating a worktree. Inspected locally: gh 2.103.0, codex-cli 0.164.0, and Claude
Code 2.1.309. Claude must support its `/code-review` skill and `auto` permission
mode. Use authenticated CLI installations (`gh auth login`, `codex login`, and
`claude auth login`). For HTTPS Git authentication through `GH_TOKEN`, configure
`gh auth setup-git`.

The intended environment is a container sandbox (such as podman) and trusted
organization repositories. Project agent settings load normally. The CLI requires
confirmation when the PR changes agent configuration. Claude defaults to `auto`;
use `PR_REVIEW_CLAUDE_PERMISSION_MODE=dontAsk` when running directly on a host.

## Use

Run anywhere in a checkout, including a secondary worktree:

```bash
pr-review 123
pr-review https://github.com/org/repository/pull/123 --no-publish
pr-review 123 --repo org/repository
pr-review 123 --repo github.example.com/org/repository
pr-review 123 --codex-model gpt-6-astra --claude-model fable
pr-review 123 --reviewers codex --codex-model gpt-6-luna --codex-effort medium
pr-review 123 --processing-provider codex --claude-effort medium
pr-review 123 --resume /absolute/path/to/run
```

Repository discovery uses `gh repo view`; specify `--repo` when it is ambiguous.
Exactly one Git remote must match the target repository. Fork PRs are fetched
through the base repository's `refs/pull/<N>/head`, without adding a fork remote.
New reviews default to both reviewers, `gpt-6-astra` and `fable`, at high effort,
with Claude processing. `--reviewers claude` or `--reviewers codex` selects one
reviewer: it produces structured findings directly and handles discussion matching,
without an adversarial synthesis phase. `--processing-provider claude|codex`
chooses synthesis and discussion matching when both reviewers are enabled; its
model and effort are inherited from that provider's review settings. Thread
adaptation and fresh-discussion reassessment use the same provider.

Settings are retained per review, including testing policy and effort. Resume uses
the pending/current generation's settings unless explicitly overridden. Changing
only the processing provider reuses compatible independent reports. Completed
reviews and human edits remain available in history if a replacement fails.

Before fetching or creating the review worktree, every run (including `--resume`)
makes a tiny live request through each enabled CLI: Claude `haiku`, then Codex
`gpt-6-luna` ([Codex model pricing](https://learn.chatgpt.com/docs/pricing)).
These requests consume a small amount of usage and catch stale logins that local
auth-status commands can miss. They run in a temporary directory with a fixed
diagnostic prompt and no PR content. Failed requests or unsuccessful structured
responses stop the run before any reviews. Raw results and stderr are saved in
`logs/{claude,codex}_auth_probe.*`; diagnostics distinguish recognized auth,
quota, model-access, network and timeout failures without printing raw errors.

Probes have a 60-second timeout each (`PR_REVIEW_AUTH_TIMEOUT` overrides it).
If your account lacks Luna access, set `PR_REVIEW_CODEX_PROBE_MODEL` to an
inexpensive model it supports; there is no automatic fallback to a costlier model.
A successful probe confirms live access to that model at that moment; review-model
permissions and credentials expiring later can still cause a later failure.

The CLI retains a detached worktree next to the main checkout, named
`<repository>-review-pr-<number>`. It uses private refs under `refs/pr-review/` and
does not update caller branches or remote-tracking refs. It holds an exclusive
repository/PR lock throughout the run. When the worktree already exists, the CLI
automatically resumes the unique saved run matching its repository, PR number,
Git checkout, worktree path and checked-out commit. It prints the selected run and
retains the normal snapshot and worktree-integrity checks. Completed phases are
reused only when their inputs still match. If no run matches or several match,
it stops with guidance; `--resume` explicitly selects a run, including one stored
outside the current state directory. Existing worktrees are never overwritten.

When the PR head or merge-base changes, rerunning automatically updates the review.
A separate working attempt is created for the new review. The earlier artifacts
(including selections, human edits and publication receipts) remain available,
and its worktree is moved to
`<repository>-review-pr-<number>-snapshot-<id>`. Untracked and ignored files stay
with that worktree; tracked modifications stop the update for inspection. The
usual worktree path is then prepared at the new commit. A saved update journal
allows an interrupted move/setup to finish on the next invocation, including when
the usual worktree path is temporarily absent. No manual worktree removal is needed.

The selected review pipeline and live GitHub discussion matching run
afresh for an updated snapshot. There is no old-versus-new finding comparison,
extra comparison model call, or automatic transfer of selections or draft text.
An unedited earlier review needs no further action.

Both independent reviewers use **focused verification**: code tracing, small
reproductions, and tests selected to check a particular finding. Each reviewer is
instructed to allow 60 seconds per check and five minutes of cumulative
verification time, avoid repository-wide test/build/lint sweeps, and report
incomplete checks instead of
restarting them with longer limits. This also applies to review skills and
delegated work. These are model instructions; they are not an enforced process
deadline. `PR_REVIEW_MODEL_TIMEOUT` remains the optional hard
limit for an entire model invocation.

Synthesis (or the structured review in single-provider mode) defaults to the same
focused limits. Use `--synthesis-tests full` to permit one standard full-suite
attempt there, with an instructed limit of 30 minutes and no automatic retries.
The independent reviewers remain focused, so this does not authorize a suite run
in each stage. Focused follow-up checks retain the five-minute allowance. The
selected policy is saved for resume; new reviews default to `focused`. Changing it
refreshes synthesis and discussion matching while reusing unchanged independent
reviews. The previous completed generation and human edits remain in history.

Claude receives independent-review instructions through its appended system
prompt; Codex receives them through
[`developer_instructions`](https://learn.chatgpt.com/docs/config-file/config-reference#developer_instructions),
while retaining the native `--base` review target. Policy changes are included in
review cache identities, so changed instructions trigger a replacement review.

If earlier drafts contain human edits, the CLI shows a local report at
`publication/previous-edits.md`. Use `[u]` to view those drafts, then `[e ID]` or `[e]`
to manually reuse relevant text in the current review. Earlier proposal IDs and
locations belong to their original commit; check them before copying. The report
is built locally after the fresh review, and earlier edited text is not given to
the models. Proposal edits are detected against their generated text; combined
body edits are detected using the generated-body hash (or reconstruction for
older runs). All archived drafts remain available whether edited or not.

Matching against actual GitHub discussion still prevents redundant posts and
provides context for replies. Local unpublished drafts are not treated as GitHub
discussion. Each updated snapshot gets fresh publication markers, and publication
still requires confirmation. Ordinary resumes of the same snapshot preserve the
current selections and edits.

Cache keys cover each stage's Git snapshot, model command, CLI version, prompts,
schemas and input artifacts. Editing a draft, changing publication UI code, or
adjusting timeout/budget limits does not rerun completed reviews. Older saved
runs are upgraded when their recorded commands, inputs and output hashes can be
verified; no model call is needed for that check. Model or prompt changes still
invalidate the affected stages.

Replacement reviews are built under `<session-directory>-history/attempt-<id>/`.
The previous review, edited drafts, and completed review-stage records stay in
place. A changed independent review restarts that phase and its downstream stages;
restarting Claude also restarts Codex to preserve independence. The candidate
contains only the reports appropriate to those stages.

Only after the selected review pipeline, discussion matching, and draft preparation
succeed does an atomic `.current-review` symlink update make the replacement
current. Ctrl-C or a failure leaves the previous review available and the candidate
pending. Rerunning the same PR or using `--resume` automatically continues that
candidate, reusing its successful stages. The CLI prints both locations after a
failure. Interrupted promotion is recoverable without rerunning successful models.
Normal draft editing and menu actions operate on the current review.

Changing models or processing settings after confirmed publication creates a
replacement with a fresh publication ledger, even on the same commit. Earlier
posts, receipts and edited drafts remain in their original review history.
The new matcher reads those posts as part of the GitHub discussion; reused finding
IDs cannot mark new issues as already posted. Uncertain publication must be
resolved in the previous review before replacing its analysis.

The initial session and its replacement attempts contain private artifacts. The CLI
prints the exact active artifact directory. Initial sessions live under
`${XDG_STATE_HOME:-~/.local/state}/pr-review/<host>/<owner>/<repo>/pr-<N>-<time>-<id>/`:

- `manifest.json`: identity, snapshot, tools, models, phase fingerprints and status.
- `context/`: PR metadata, paginated discussion, reviews, inline comments, CI, local diff.
- `discussion/`: versioned complete conversations and resolved/outdated thread metadata.
- `reviews/`: independent reports, raw Claude events, structured synthesis and final Markdown.
- `prompts/`: the rendered synthesis prompt, referencing explicit local artifacts.
- `logs/`: model events, errors, command arguments and integrity snapshots.
- `publication/`: contribution proposals, editable drafts, per-action status, exact
  submitted bodies, API responses, and archived drafts.

Slow review models are allowed to finish. Heartbeats say "Process running" and show
elapsed time and time since stdout or stderr changed; process liveness does not
prove progress. A warning appears after 60 seconds without output.
Synthesis and discussion matching stream Claude events, including partial messages,
so activity is visible before the final structured result. Raw events remain in
their existing `logs/*.raw.json` files as newline-delimited JSON. This logging
change preserves the cache identity of completed buffered-JSON reviews.
Recognized terminal events (including Codex's "not a git repository" error and `turn.failed`)
stop the model, escalating from TERM to KILL after five seconds if needed. Other
error events are flagged while allowing retries. There is **no default timeout or
budget limit** for a silent review without a terminal event. Before each review
invocation, Git repository identity is checked in the model's filtered environment;
diagnostics are retained in `logs/<stage>.git-preflight.log[.stderr]`.
Ctrl-C forwards the signal to the model process group, escalates to TERM and KILL
with bounded waits if necessary, and records an interrupted stage. Successful phases are
reused only when their snapshot, inputs, model, version, policy and output hashes
still match. Invalidated independent reviews are archived and restarted as described
above. Unchanged contribution proposals retain human edits
and selections across refreshes. Changed proposals are rebuilt with safe defaults
and the old drafts are archived. Legacy version-1 runs remain readable but require
a new run for discussion-aware publication.

Optional environment settings:

| Variable | Meaning |
| --- | --- |
| `PR_REVIEW_MODEL_TIMEOUT` | Positive integer seconds per model invocation; unset by default |
| `PR_REVIEW_CLAUDE_BUDGET` | Maximum USD per Claude invocation; unset by default |
| `PR_REVIEW_CLAUDE_PERMISSION_MODE` | `auto` (default), `dontAsk`, `manual`, or `plan` |
| `XDG_STATE_HOME` | Artifact location; must be outside the reviewed repository |
| `VISUAL`, `EDITOR` | Editor command and simple arguments; default `vi` |

## Publication and safeguards

The final screen shows each finding’s title and assigned destination, including
the file/line, thread excerpt and link for replies. It classifies findings by what
they add to the conversation:

| Classification | Default |
| --- | --- |
| New issue | Selected for the main review |
| Useful addition | Selected as a reply to the existing inline thread |
| Revisit a resolved or explicitly concluded discussion | Unselected; inspect the new evidence first |
| Duplicate with no material addition | Suppressed, but inspectable locally |
| Uncertain relationship or evidence | Unselected |

Replies consider the complete conversation, including later corrections, questions,
and conclusions. They explain what is new instead of repeating the opening comment.
Selected additions to one thread become one reply. Follow-ups to timeline comments
or review bodies go in the main review with links. Models never resolve or reopen
threads. The Emacs Conversations view supports explicit reviewer resolution and
reopening. An outdated comment is not automatically a resolved issue.

Use `t p1` to toggle a proposal, `i p1` to inspect its full discussion, `e p1` to
edit its draft, `s` to inspect suppressed duplicates, `v` to preview the batch, and
`f` to display the full local review. The default screen stays compact.
Plain `e` edits the main review body. Use `c` to publish all selected items at their
automatically assigned destinations: thread additions become replies, while new
items go together in a COMMENT review. The menu counts the replies and main-review
items that will be posted. Replies alone do not generate a redundant summary.
Use `v` to preview exact drafts and destinations without posting, or `i p1` to
inspect an individual proposal, its matching rationale and the full discussion.
Nothing new selected means no COMMENT is posted. Explicit APPROVE or
REQUEST_CHANGES verdicts remain available. The legacy `p` replies-only shortcut
is still accepted, but is omitted from the main menu; toggling individual items
provides selection control without choosing a separate publication mode.

The model proposes semantic matches; validating a real thread ID cannot prove the
match is correct. Duplicates stay suppressed, and uncertain matches and follow-ups
to concluded discussions start unselected. Uncertainty remains visible even when
a matched thread is resolved. Inspect, edit or deselect proposals before the final
confirmation. Follow-ups to timeline comments or review bodies appear in the main
review with source links because they have no inline reply destination.

Pressing Enter chooses `n` and exits the menu with drafts saved. Self-authored PRs offer COMMENT only.
Approving with blocking
findings requires an additional confirmation. Closed/merged PRs, `--no-publish`,
and non-TTY stdin disable publication. Agent-configuration changes still require
interactive trust confirmation, even with `--no-publish`.

Duplicate suppression does not erase a valid blocker or make approval safe. Fresh
discussion that challenges validity triggers synthesis reassessment without rerunning
the independent reviewers. If the conflict remains unresolved, publication pauses.

Before confirming and before each write, the CLI refreshes discussion and checks the
PR identity, head, open state, merge-base and destination permissions. Changed
discussion triggers rematching and a return to the selection screen. Incomplete
discussion, thread state or pagination pauses publication; local reports survive.

One final `yes` confirms the exact bodies and destinations in the displayed batch.
Replies are posted sequentially, followed by the main review. The main review is
pinned to the reviewed head; replies identify that commit in their text. Normal
base advancement is informational. Per-action hidden markers prevent duplicate
posts after ambiguous responses or interrupted submissions. Resume skips successful
actions and requires confirmation of the remaining actions.

GitHub has no atomic transaction for this batch: failures preserve already-published
posts and report partial completion. Freshness checks reduce but cannot eliminate
the small concurrent-comment window between a check and a POST.

Models receive filtered environments: credential-like variable names are removed,
except each CLI's own authentication variables. Only removed names are recorded.
Temporary Git config overrides (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`,
`GIT_CONFIG_VALUE_*` and `GIT_CONFIG_PARAMETERS`) are removed together, avoiding
both credential leakage and incomplete config entries that break Git discovery.
On-disk credentials remain accessible to the CLIs. Code and review context are sent
to the selected model providers. Use this tool only on repositories you trust.

The orchestrator does not run repository tests, builds or hooks. Models may run
investigative commands under their configured permissions. After each invocation in the review worktree,
changes to HEAD, tracked files, the index or snapshot refs stop the run and block
publication. Generated untracked/ignored files are retained and disclosed.

CI includes Actions workflow runs and legacy commit statuses for the reviewed head.
Third-party GitHub App check runs are excluded. Empty results never mean CI passed;
unavailable endpoints are reported explicitly. Suggested fine-grained token
repository permissions are Metadata/Contents/Pull requests/Actions/Commit statuses
read, plus Pull requests write for publication. Read access does not verify write
permission; GitHub may reject the chosen review operation.

## Verify

```bash
# Deterministic: mocked gh/codex/claude, real local bare Git remote and worktrees.
# Requires bats 1.5+, shellcheck, and Python 3 (PTY/signal test harnesses only).
make check

# Optional: actual authenticated model CLIs, tiny synthetic repository, mocked
# GitHub. Incurs model usage, never publishes, and retains artifacts under /tmp.
tests/smoke.sh

# Optional semantic evaluation: real Claude matching against curated conversations.
# Other model stages and GitHub are mocked. Incurs model usage; never publishes.
tests/evaluate-contributions.sh
```

The suite covers authentication, flags, independence, snapshots, worktree integrity,
credential filtering, partial context, resume, interrupts, locks and publication
through a real pseudo-terminal. Re-run the live smoke test after upgrading model
CLIs: help checks verify syntax, while model/skill availability is server-dependent.

Live smoke verified on 2026-10-23 with Codex 0.164.0 and Claude 2.1.309: independent
Claude review in `auto` mode, Codex review, and synthesis with `--json-schema` and
`--add-dir` all completed. Tracked source was unchanged; a generated Python cache
was retained and disclosed. GitHub was mocked; no review was published.

The discussion-aware matching pass was also evaluated with real Claude against six
curated cases: paraphrased duplicates, contextual additions, resolved follow-ups,
different bugs on the same line, coverage in later replies, and conversations
concluded without a resolution flag. All six matched the expected classifications;
the contextual reply answered the author's open question and acknowledged the
earlier fix. These are sample-based checks, not a guarantee of model judgment.

## Magit and Forge frontend

The optional [Emacs package](emacs/README.md) shows review comments directly in
their code and discussion context. Use native sections to stage, unstage, edit,
correct placement, browse saved generations, and publish exact inline comments.
`M-x magit-auto-review` opens a saved review without running models. A versioned
[JSON API](docs/api.md) keeps the frontend independent of private state files.

The package lives alongside the backend under `emacs/`, with separate checks and
a [Guix package definition](guix.scm). The ordinary CLI remains independently
usable. Run `make check-backend`, `make check-emacs`, or `make check-all`.

## Following up on review conversations

In Emacs, `C` opens all saved conversations, with unresolved threads (including
outdated ones) separate from resolved threads. `c` creates an attached reply
draft; `x` resolves a conversation and `X` reopens it. These explicit thread
actions refresh and validate GitHub state first. They do not change your review
verdict. Opening the view and `g` remain local; `G` refreshes GitHub.

`F` checks the conversation at point or selected conversation headings against
the latest pushed code, using the saved processing provider. This creates a
limited follow-up generation without running another full PR review. It reports
Addressed, Still present, No longer applicable, or Uncertain, with evidence and
verification limits. Read the assessments before resolving or replying. No
assessment automatically creates a draft, posts a reply, or changes thread state.

The backend equivalent is `pr-review api` with `command: "followup"`; the CLI
also accepts repeated `--followup-thread thread:KEY` options when resuming an
existing review. Follow-up always uses focused checks, regardless of the full
review's synthesis testing setting. Earlier reports, edits and receipts remain
in history. Follow-up does not transfer old drafts or imply whole-PR approval.

New API/Emacs draft generations default to Comment. Approve and Request changes
require an explicit reviewer choice, separate from the model recommendation and
the last submitted GitHub review. GitHub review status is not overall merge
eligibility. See the [workflow overview](docs/github-review-process-overview.md)
for the state model and features delegated to GitHub/Forge.
