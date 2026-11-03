# Auto-review manual

This manual covers the shared backend and terminal workflows. Start with the
[project README](../README.md) for installation and an Emacs quickstart, or use
the [Emacs reference](../emacs/README.md) for detailed keys and buffer behavior.
The [JSON API reference](api.md) describes the frontend/backend contract.

## Contents

- [Requirements and authentication setup](#requirements-and-authentication-setup)
- [Running a review](#running-a-review)
- [Automatic reviews](#automatic-reviews)
- [Suggested changes](#suggested-changes)
- [Configuration](#configuration)
- [Live authentication checks](#live-authentication-checks)
- [Verification policy](#verification-policy)
- [Reviewer publication](#reviewer-publication)
- [Conversation follow-up](#conversation-follow-up)
- [PR owner workflow](#pr-owner-workflow)
- [Worktrees and updated snapshots](#worktrees-and-updated-snapshots)
- [Resume, generations, and history](#resume-generations-and-history)
- [Saved artifacts](#saved-artifacts)
- [Compatibility with older sessions](#compatibility-with-older-sessions)
- [Trust, credentials, and GitHub access](#trust-credentials-and-github-access)
- [Progress and troubleshooting](#progress-and-troubleshooting)

## Requirements and authentication setup

Requires Linux, Python 3.9+, Git, `gh`, and the model CLIs for enabled reviewers.
The backend uses the Python standard library; no pip installation, Bash, jq,
external `flock`, or external `timeout` is needed at runtime.
The CLI checks capabilities through help output before creating a worktree.
Claude must support its `/code-review` skill and `auto` permission mode.
Use authenticated CLI installations (`gh auth login`, `codex login`, and
`claude auth login`). For HTTPS Git authentication through `GH_TOKEN`, configure
`gh auth setup-git`.

The intended environment is a container sandbox (such as podman) and trusted
organization repositories. Every Codex model invocation (including probes,
review, planning, fixes, suggestions, and verification) explicitly uses
`sandbox_mode=danger-full-access` and `approval_policy=never`. Independent
reviewers use disposable clones, separate scratch and output, and fresh provider
homes. Claude invocations set `sandbox.enabled=false`, separately from permission
modes and tool denials. No nested bubblewrap, namespace or procfs mount is required.
The backend calls plain `codex`; no `codex-dangerous` wrapper is required.
Project agent settings load normally, and configuration changes still require trust.
Selected authentication/configuration files and provider skills are copied into
each private home; provider session histories are not copied. Copies are removed
when the stage ends. The application does not change global user settings.
Managed provider policy retains precedence; deployments enforcing a conflicting
sandbox policy are incompatible with this execution mode and must be reported as
such, not bypassed.

## Running a review

Run anywhere in a checkout, including a secondary worktree:

```bash
pr-review 123
pr-review https://github.com/org/repository/pull/123 --no-publish
pr-review 123 --repo org/repository
pr-review 123 --repo github.example.com/org/repository
pr-review 123 --codex-model gpt-6.1-sol --claude-model opus
pr-review 123 --reviewers codex --codex-model gpt-6-luna --codex-effort medium
pr-review 123 --processing-provider codex --claude-effort medium
pr-review 123 --reviewers claude,claude --processing-provider claude --reviewer-2-model sonnet --reviewer-2-effort medium
pr-review 123 --reviewers codex,codex --processing-provider codex --reviewer-2-model gpt-6.1-terra --reviewer-2-effort high
pr-review 123 --economical
pr-review 123 --expensive
pr-review 123 --economical --codex-effort medium
pr-review 123 --resume /absolute/path/to/run
pr-review drafts /absolute/path/to/run  # inspect/edit saved drafts; no model calls
```

Repository discovery uses `gh repo view`; specify `--repo` when it is ambiguous.
Exactly one Git remote must match the target repository. Fork PRs are fetched
through the base repository's `refs/pull/<N>/head`, without adding a fork remote.
New reviews default to both reviewers, `gpt-6.1-sol` and `opus`, at high effort,
with Codex/Sol processing. `--reviewers` takes one or two ordered providers,
including two instances of the same provider. Slot-specific `--reviewer-1-model`,
`--reviewer-2-model`, and corresponding `--reviewer-1-effort` and
`--reviewer-2-effort` options configure the reviews independently. Individual
providers can also be changed with `--reviewer-1-provider` and
`--reviewer-2-provider`.
`--processing-provider`, `--processing-model` and `--processing-effort` select
the separate synthesis and discussion role. A fresh single-reviewer selection
defaults to that provider; if the reviewer and processing choices differ, the
reviewer runs first and its report is supplied to the structured review. Thread
adaptation and fresh-discussion reassessment use the same processing choice.

`--economical` supplies new-review defaults: Haiku + GPT-6.1 Terra, both at high
effort, with Terra processing. `--expensive` instead supplies Fable + GPT-6 Astra,
both at high effort, with Astra processing. These preset flags are mutually
exclusive. Explicit model, effort, reviewer, and processing flags override the
preset regardless of argument order. Testing and suggested-change options are
unaffected. Saved review settings take precedence over this defaults
preset: use explicit flags (or the Emacs setup action) to change an existing review.
The expanded choices are saved as ordinary settings; there is no persistent mode
that prevents later edits. These reviewer flags work with interactive analysis,
`--no-publish`, and `--auto-publish`; they do not configure interactive Owner sessions. For `fix-issue`,
the same flags configure acceptance review (see [Issue fixing](#issue-fixing)).

Settings are retained per review, including testing policy and effort. Resume uses
the pending/current generation's settings unless explicitly overridden. Changing
only the processing provider reuses compatible independent reports. Completed
reviews and human edits remain available in history if a replacement fails.

## Automatic reviews

```sh
pr-review 123 --auto-publish
pr-review 123 --auto-publish --reviewers codex --codex-model gpt-6-luna --codex-effort medium --json
pr-review 123 --auto-publish --resume /absolute/path/to/run --json
```

`--auto-publish` authorizes publication for this invocation only. It never opens
a menu or asks a question, even on a terminal. Ordinary CLI and Emacs runs keep
their explicit staging and confirmation workflow. The flag cannot be combined
with `--no-publish` or `--followup-thread`; `--json` requires `--auto-publish`.
Settings use backend defaults, saved pending/current settings, then explicit
flags. Enabled providers' model/effort choices are validated from fresh metadata.

The completed analysis must be current. Automatically suppressed duplicates and
confirmed published comments are excluded; all remaining findings must pass bulk
staging checks. An uncertain/revisit finding, resolved/outdated thread reply,
missing/invalid placement, empty comment, partially covered finding, or changed
discussion needing inspection stops the entire batch before staging. No findings,
or only duplicates/already published findings, produces a successful no-op after
freshness validation. No completion comment is posted.

Eligible comments are staged and published with COMMENT only. Model approval or
request-changes recommendations are not adopted. Saved manual verdicts and
unpublished manual decisions require completion in Emacs. Older drafts with
unknown mutation authorship also require Emacs; read-only inspection does not
count as a manual decision. Completed historical manual work does not permanently
prevent a later automatic generation.

Inline comments form a review; thread replies are separate requests. A failure
or a context change between requests can therefore leave partial publication.
The automatic run reads receipts back once after a submission failure, but does
not resend. Later automatic invocations also check outstanding intent before
analysis and leave an unsent or uncertain remainder for Emacs. Confirmed comments
and verdicts are not repeated to finish the remainder. Receipt-based duplicate
prevention covers retries of the same generation; across new analysis generations,
avoiding equivalent findings depends on model discussion matching.

Progress goes to stderr. With `--json`, stdout contains one final object with
`outcome`, `exit_code`, `reason`, `affected`, and `receipts`, plus available
`session`, `generation`, `operation_id`, `log_path`, and recovery `guidance`.
`affected` contains comment IDs and reasons. Trust failures also include
`trusted_snapshot` and `agent_config_paths`; no blanket trust is inferred.

| Exit | Meaning |
| --- | --- |
| 0 | `published` (including confirmed receipt recovery) or `noop` |
| 1 | Execution failure |
| 2 | Invalid command-line arguments |
| 3 | Needs attention, including paused analysis or an unsent batch |
| 4 | Confirmed partial publication with an unsent remainder |
| 5 | Publication remains uncertain; takes precedence over partial success |
| 128 + signal | Interrupted, for example 130 for Ctrl-C |

Open the same PR in Emacs to continue. A persistent summary identifies the stop
and links to affected findings. Inspect and individually stage with `s`, exclude
with `k`, correct placement with `a`, or edit with `e`. For changed discussion,
inspect it and acknowledge with `i` before staging. `R r` resumes incomplete
analysis; `P` offers publication and receipt recovery; `L` reads the saved operation
log. Agent-configuration failures retain the exact snapshot and paths for explicit
trust with `T`. Opening the buffer does not resume models or publish anything.

Automatic run records live under the session's `automatic-runs/`, separately from
generation drafts. Reports, drafts, generations, receipts, and prior run records
are retained. A paused first analysis does not create an empty draft ledger.

## Suggested changes

Suggested changes are opt-in for both interactive and automatic reviews:

```sh
pr-review 123 --suggested-changes
pr-review 123 --suggested-changes --auto-publish --json
```

The setting is saved with the review. Use `--no-suggested-changes` to disable
it for subsequent analysis; existing saved drafts and history remain available.
In Emacs reviewer setup, `R s` toggles suggestions, `R r` runs analysis, and
`R P` explicitly runs analysis **and publishes automatically** as a COMMENT
review. Automatic publication is never a saved default. These reviewer controls
are separate from Owner mode's automatic fix-and-commit workflow.

A suggestion replaces the exact inclusive line range attached to a new inline
comment. The body explains the finding; a generated `suggestion` code fence
contains the replacement. GitHub lets the PR owner inspect and apply it. The
review tool itself does not commit or push the replacement.

The first version supports one complete, independent fix per finding, within
one contiguous RIGHT-side range of a single diff hunk. Multiline replacements
and deletions are supported, including ranges in files already added by the PR.
Thread replies, overall comments, combined findings, file creation/renaming,
and fixes requiring coordinated changes elsewhere remain ordinary findings.
Source must be regular UTF-8 text with LF endings and a final newline. Replacements
containing a Markdown closing fence are rejected rather than published inaccurately.

Generation follows discussion matching. Each proposed replacement then receives
a **separate verification invocation**, using the configured processing provider,
model, and effort. Verification runs against the exact replacement in an isolated
checkout and follows the focused testing policy, even when synthesis uses `full`.
Multiple individually accepted suggestions also receive a combined
independence/interaction check. Rejected suggestions remain blocked, but do not
prevent this check of the accepted subset. You can stage that subset interactively;
automatic publication still requires every finding to be ready. A combined result
is shown as accepted only for suggestion versions included in that check.
This adds model time and cost. Evidence explicitly distinguishes executed checks
from acceptance by inspection; inspection-only acceptance requires a justification.
Verification reduces risk but does not guarantee correctness.

A setup failure can be resolved by a successful rerun within the same verification
budget and permitted environment. Both attempts remain visible, with the rerun
identifying the setup check it supersedes. An executed failing test cannot be
superseded this way; unresolved failures still block acceptance. Older saved
verification failures require an explicit new verification, not automatic acceptance.

Both frontends show the exact target, before/after diff, evidence, and blockers:

| Action | Emacs reviewer | Interactive CLI |
| --- | --- | --- |
| Inspect | Expand the finding | `i ID` |
| Edit generated replacement | `S e`, then `C-c C-c` to save | `se ID` |
| Verify active suggestions | `S v` | `sv` |
| Remove suggestion, retain finding | `S k` | `sx ID` |

Replacement text must retain its final newline; empty replacement text deletes
the selected range. Editing explanation, replacement, or target unstages the draft
and requires verification again. Stale editor saves retain the replacement buffer
or CLI recovery file. Manual creation of suggestions is not supported yet. Raw
suggestion fences pasted into ordinary bodies are blocked so they cannot bypass
source checks and verification.

Recovering a saved version into the same draft restores its placement and
suggestion together, with fresh verification required. Recovering wording into a
different draft preserves that destination's finding identity and removes its
attached suggestion. A new recovered draft within the same analysis can retain
the source suggestion; recovery from an older analysis copies wording only,
because finding IDs may have been reused. Original suggestions remain in history.

An unsuitable complex fix can omit a suggestion without blocking its valid ordinary
finding. An attached invalid, rejected, or uncertain suggestion blocks that comment;
in automatic mode it blocks the **entire batch before publication**. Generation
failures also remain visible and block publication. Resume interrupted analysis,
edit and verify the replacement, remove the suggestion, or suppress the finding.
Resuming retries failed generation without moving the finding to an invalid
candidate's target. Regenerated candidates remain in saved history; this automatic
history alone does not count as a manual decision. Untagged legacy history remains
conservative and requires interactive completion.
If HEAD changed, rerun analysis against the new snapshot. Existing restrictions
on automatic publication after manual intervention still apply: finish inspected
manual changes interactively.

Completed verification, candidates, evidence, and logs are retained with the saved
review. Unchanged work is reused on resume, and publication receipts retain the
existing retry and uncertain-submission recovery behavior. Repository tests use
mocked GitHub APIs; validate actual GitHub rendering and application on a designated
test PR before relying on unattended suggestions in a new deployment.

## Configuration

Review flags are shown by `pr-review --help`; owner operations by
`pr-review owner --help`. The following environment settings supplement those flags.

| Variable | Meaning |
| --- | --- |
| `PR_REVIEW_MODEL_TIMEOUT` | Positive integer seconds per model invocation; unset by default |
| `PR_REVIEW_CLAUDE_BUDGET` | Maximum USD per Claude invocation; unset by default |
| `PR_REVIEW_CLAUDE_PERMISSION_MODE` | `auto` (default), `dontAsk`, `manual`, or `plan` |
| `XDG_STATE_HOME` | Artifact location; must be outside the reviewed repository |
| `VISUAL`, `EDITOR` | Editor command and simple arguments; default `vi` |
| `PR_REVIEW_MAX_EVENT_BYTES` | Maximum provider JSON record size in bytes; default 67108864 (64 MiB). Increase for larger valid results. Stderr is logged without this record limit. |
| `PR_REVIEW_AUTH_TIMEOUT` | Positive integer seconds for each live probe and local authentication check; default 60 |
| `PR_REVIEW_CODEX_PROBE_MODEL` | Codex live authentication probe model; default `gpt-6-luna` |
| `PR_REVIEW_COMMIT_TIMEOUT` | Positive integer seconds per automatic owner commit; default 120 |

## Live authentication checks

Before fetching or creating the review worktree, every run (including `--resume`)
makes a tiny live request through each enabled CLI: Claude `haiku`, then Codex
`gpt-6-luna`.
These requests consume a small amount of usage and catch stale logins that local
auth-status commands can miss. They run in a temporary directory with a fixed
diagnostic prompt and no PR content. Failed requests or unsuccessful structured
responses stop the run before any reviews. Raw results and stderr are saved in
`logs/{claude,codex}_auth_probe.*`; diagnostics distinguish recognized auth,
quota, model-access, network and timeout failures without printing raw errors.

Probes have a 60-second timeout each (`PR_REVIEW_AUTH_TIMEOUT` overrides it).
Local authentication checks use that deadline too. Ordinary Git and GitHub
operations, including fetches, worktree creation, and pagination, have no fixed
deadline; they remain cancellable. Model discovery retains its separate deadline.
If your account lacks Luna access, set `PR_REVIEW_CODEX_PROBE_MODEL` to an
inexpensive model it supports; there is no automatic fallback to a costlier model.
A successful probe confirms live access to that model at that moment; review-model
permissions and credentials expiring later can still cause a later failure.

## Verification policy

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
prompt; Codex receives them through `developer_instructions`, while retaining
the native `--base` review target. Policy changes are included in
review cache identities, so changed instructions trigger a replacement review.

## Reviewer publication

The terminal and Emacs use the same drafts, optimistic revisions, and publisher.
All fresh proposals start **Unstaged**; duplicates start **Suppressed**. A valid
structured location becomes an inline placement. Matches to an existing thread
become replies. A finding without a usable location stays unplaced until you
choose code, a thread, or an explicit overall assessment.

| Terminal command | Action |
| --- | --- |
| `i ID` / `e ID` | Inspect full context / edit exact draft text |
| `a ID` | Choose code, a thread, `overall`, or restore `auto` placement |
| `s ID` / `u ID` | Stage / unstage one draft |
| `k ID` / `r ID` | Suppress / restore a draft |
| `ack ID` | Acknowledge changed discussion after inspection |
| `new` | Compose a separate overall assessment |
| `v COMMENT`, `v APPROVE`, `v REQUEST_CHANGES` | Explicitly select a verdict |
| `p` | Refresh, validate, preview, and ask to publish the staged batch |
| `refresh` / `resolve` | Refresh discussion / reconcile uncertain submissions |
| `history`, `show GENERATION`, `current`, `docs` | Inspect saved generations and documents |
| `recover GENERATION ID`, `recover-doc GENERATION NAME` | Copy saved text into a new Unstaged draft |
| `q` or Enter | Exit with saved drafts retained |

Reply drafts consider the complete conversation, including later corrections and
conclusions. Separate drafts stay separate when published; inline comments are
submitted together in one review, and one explicit overall assessment may supply
its body. Models never resolve or reopen threads. The Emacs Conversations view
supports explicit reviewer resolution and reopening. Outdated is not the same as
resolved.

`p` shows the exact staged text and destinations. Only a final `yes` publishes.
Replies alone create no redundant summary. An otherwise empty review requires an
explicit verdict choice; model recommendations never select one automatically.
Approval previews disclose retained blocking findings. Self-authored PRs permit
Comment only. Closed/merged PRs remain inspectable, but cannot be published.
`--no-publish` and non-TTY review invocation do not enter the menu. Agent config
changes still require explicit trust for the snapshot.

Before confirmation and each write, the backend checks discussion completeness,
account, head, open state, merge-base, and destination permissions. Changed
conversations unstage affected drafts and require inspection and renewed staging;
publication never runs a model or rewrites approved text. Validity reassessment
happens only during an explicit review run. Unresolved conflicts pause publication.

The shared `publication/drafts.json` owns draft versions, intent, exact payloads,
and receipts. Intent is saved before a network write. Hidden markers identify the
reviewed head, generation, and action. `resolve` reconciles lost responses without
blindly reposting; a known parent review with missing inline receipts is never
resubmitted. Successful actions survive partial failures, and remaining actions
require another preview and confirmation. Ordinary base advancement is informational.

GitHub has no atomic batch transaction. Freshness checks reduce but cannot eliminate
the small window between the last check and each POST. The editor keeps a private
recovery file if saving fails or the generation/revision changes while editing.

## Conversation follow-up

In Emacs, `C` opens all saved conversations, with unresolved threads (including
outdated ones) separate from resolved threads. `c` composes an attached reply;
`C-c C-c` saves it locally and `C-c C-k` cancels it. Each reply is a separate
draft that can be staged or suppressed independently. `x` resolves a conversation
and `X` reopens it. These explicit thread
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

New draft generations in both interfaces default to Comment. Approve and Request changes
require an explicit reviewer choice, separate from the model recommendation and
the last submitted GitHub review. GitHub review status is not overall merge
eligibility. See the [workflow overview](github-review-process-overview.md)
for the state model and features delegated to GitHub/Forge.

## PR owner workflow

Owner mode opens independently of saved reviewer runs. It keeps complete incoming
discussions, unread/changed state, local triage, review verdicts, thread resolution,
and outdated-code status separate. Opening uses saved discussions. Local reload
(`g`) also checks Git commits; `G` explicitly reads GitHub without a model call.

### Importing findings

To fix findings from your own local analysis, select a finding, status group, or
sibling headings in Reviewer and press `o` (**Send to Owner for fixing**). Owner
imports exactly those items and focuses the first one; press `f` to fix the item
at point. For a batch, press `f` on an Owner group heading or explicitly select
sibling headings. Importing does not leave an active selection over the group.
The handoff copies saved wording, original findings, and code/discussion context
without changing reviewer staging or publishing anything. Repeating it does not
duplicate unchanged findings. Explicitly re-importing an edited draft retains the
previous copy and marks dependent fix assessments stale.
When the handoff creates an Owner session, its initial roles use the saved
review's processing provider, model, and effort. Existing Owner role settings
remain independent and are not overwritten by later imports.

You can also fix GitHub feedback you posted yourself. After `G`, it appears under
**Your GitHub comments** without an unread alert. Select it and press `f`, or use
`U` to queue it under Attention needed. Local imported findings have no GitHub
reply destination: their fixes can be associated with commits, but do not generate
fixed-in replies. Published feedback retains its normal thread/timeline reply flow.

### Preparing and committing fixes

After a group fix finishes, its logical fixes normally become separate commits.
Related findings may share one fix. Under **Fixes**, expand the batch to see the
ordered commit rows. `m` on the batch prepares the next commit; on a commit row
or feedback item it targets that logical fix. Earlier commits must be completed
first. Magit receives the saved, verified changes and a message for just that fix.
Finish with `C-c C-c`, return to Owner, press `g`, and repeat `m` for the next fix.
Later fixes and manual edits stay unstaged, including separate changes to the
same file. Push to the displayed **Push PR** destination with `P u`.
Conflicting or unrelated existing staging is
preserved and requires manual inspection. Use `C-u m` for status only.
When the task checkpoints form a continuous chain, Owner preserves their commits
and appends an **Integration repair** commit for changes required by the final
combined review or adopted afterward and reverified. All commits are prepared
only after the final tree passes verification. Replies reference the task commit
and the integration repair; the complete series must be pushed before publication.
Intermediate commits may depend on that repair and are not independently verified
as final fixes. Missing or discontinuous checkpoints still produce one explained
combined commit. Existing whole-group commits and started combined-commit
transactions are retained without rewriting history.

Use **`C-u f`** to continue automatically through committing and reply preparation.
It completes the combined review, creates the logical commits in order, finishes
matching local findings, and stages **Fixed in HASH** replies to the original
GitHub conversations. Replies show **Awaiting push** until their commits reach
the PR. Push with Magit, then use `P` to check and preview all staged replies and
confirm publication; no separate `a` step is needed. Nothing is pushed, published,
or resolved automatically. Local findings have no GitHub reply destination.

The batch remembers **Auto commit**; `r` resumes it without repeating successful
provider work. `C-u r` selects automatic or manual completion, including enabling
it for an already verified batch. Commit failures retain verification and report
partial commit progress. Hooks and signing are honored; failures never trigger
an unsigned or hook-free retry. The per-commit timeout defaults to 120 seconds;
`PR_REVIEW_COMMIT_TIMEOUT` sets a positive integer number of seconds. An open
Magit commit editor must be finished/cancelled before starting automatic completion.
Human reply edits and explicit unstaging survive retries.

The progress log names the number of selected feedback items and distinguishes
**Fix verified** from **Fix paused**. A paused result shows completed-task counts
and the verification blocker. Its changes remain unstaged; inspect the saved fix
and resume it with `r` before using automatic commit preparation.
If implementation succeeded but review was incomplete, `r` retries that review
against the unchanged fix, then checks the combined result. Reviewers can run
focused tests with disposable files and Git repositories in an attempt's scratch
directory; the Owner checkout and its real Git index remain protected.
Review explicitly distinguishes passing verification, concrete defects requiring
changes, and verification that could not finish. A failing regression test proving
a defect enters the bounded repair pass; an unavailable test environment pauses
for verification to be retried. Resuming an unchanged rejection passes the saved
findings to the fixer if its repair attempt remains. Resume does not reset an
exhausted repair budget: inspect/adopt manual corrections or replan unfinished work.

After committing, return to Owner and press `g`. An exact match to a verified
fix automatically finishes its local findings and moves the completed fix under
**Deferred / History**, showing **Committed HASH**. Group fixes list their included
feedback when expanded. For manual fixes, push and use `a` to associate commits
and prepare replies. Automatic fixes already have their replies staged. `h` remains available for manual
completion; arbitrary splits/rebases outside the recorded sequence require inspection. `U` and explicit re-import
keep a finding queued until you finish it or make a new fix. `r` requires selecting
one fix or its feedback; the Fixes header never resumes a group.
Fix results show their next step, with verification details and earlier attempts folded separately.
`?` shows actions for the current selection; `+` opens the complete command list.

### Owner CLI

```bash
pr-review owner 123                         # open/read cached owner state
pr-review owner 123 refresh                 # collect current GitHub feedback
pr-review owner 123 settings                # inspect independent fix/review settings
pr-review owner 123 settings --settings-file /tmp/owner-settings.json
pr-review owner import --review-session /path/to/review --comment p1 --comment p2
pr-review owner 123 fix --item thread:THREAD_ID
pr-review owner 123 fix --item thread:FIRST --item thread:SECOND
pr-review owner 123 fix --item thread:THREAD_ID --verify-only
pr-review owner 123 resume --batch BATCH_ID
pr-review owner 123 fix --item thread:THREAD_ID --auto-commit
pr-review owner 123 resume --batch BATCH_ID --no-auto-commit
pr-review owner 123 diff --batch BATCH_ID --current
pr-review owner 123 resume --batch BATCH_ID --adopt
pr-review owner 123 prepare-commit --batch BATCH_ID  # stage the next logical fix; return suggested message
pr-review owner 123 prepare-commit --batch BATCH_ID --unit TASK_ID  # target one recorded commit
pr-review owner 123 reconcile               # check local commits and finish matching local findings
pr-review owner 123 associate               # after human commits and pushes
pr-review owner 123 prepare                 # inspect exact staged replies and token
pr-review owner 123 publish --confirm TOKEN
pr-review owner 123 recover                 # read back uncertain submissions
pr-review owner discover                    # saved owner sessions in this checkout
pr-review owner cancel --operation-id OPERATION_ID
```

Use item and batch IDs from the JSON responses. `--session PATH` accepts an opaque
session returned by open/discovery in place of the PR number. `--repo` selects
among repository remotes. `pr-review owner --help` lists the remaining local
triage, reply, and explicit resolve/reopen operations.

### Provider settings and verification

Settings have a `fix` and a `review` object, each with `provider`, `model` and
`effort`, an optional `final_review` object, and `verification: "focused"`.

- `fix` is the implementation session.
- `review` configures focused per-task checks only. Each task check is one
  lightweight provider call; it never uses the review engine.
- `final_review` configures the [shared review engine](#shared-review-engine) that
  verifies the combined fixes (and `fix-issue` acceptance):
  `{reviewer_slots, synthesis, synthesis_tests}`. `reviewer_slots` holds one or two
  slots (`reviewer-1`, `reviewer-2`, the same provider allowed) with independent
  provider, model and effort. `synthesis` is a `{provider, model, effort}` choice and
  `synthesis_tests` is `focused` or `full`.

New sessions use the common reviewer and synthesis defaults: Claude and Codex
reviewers with Codex synthesis, while `fix` and `review` start from the default
processing provider and are thereafter independent. A saved session or settings
file without `final_review` stays valid and single-reviewer: its `review` choice is
both the only slot and the synthesis. That effective configuration is computed when
needed, shown in the Owner view as `effective_settings` (for the session and for each
batch's frozen settings) and never written back. Fix operations that run providers
validate fresh model metadata for every role, slot and synthesis choice, check the
Codex review capability, and perform live
authentication probes; commit-only recovery needs no provider access. Opening or
editing settings does not invoke inference.

Large Owner prompts store the complete task context in each attempt’s `context/`
directory and pass an index to the provider. Feedback, accumulated diffs, and
completed-task evidence are preserved without truncation. This keeps growing
batches below the provider’s request-size limit.

The planner can combine related concerns and order tasks by dependency. Tasks run
sequentially in a dedicated `pr-owner/...` branch and linked worktree, preserving
every original feedback destination. The fixer may edit source; the planner and
reviewer are instructed not to edit source. For Codex this is a workflow rule,
not an OS-enforced read-only sandbox; the backend also checks repository
integrity. Each task gets one lightweight focused check and the combined patch gets
the engine's final verification, with at most one automatic repair at each
checkpoint. A repair changes the tree, which is a new candidate. Incomplete checks,
clarification, or remaining blocking findings pause the batch. Owner checks use
the focused 60-second-per-check/five-minute cumulative instruction limits.

Use native Magit, or ordinary Git in the displayed owner worktree, to inspect,
stage, commit, and push. The view shows the actual head repository and branch,
including forks. A unique existing matching remote is configured on initial
setup; missing/ambiguous remotes need explicit configuration in Magit. Check the
displayed PR destination when pushing the differently named owner branch.
Only the explicit automatic-completion option creates commits. The application
never pushes or resolves threads as part of fixing.

### Shared review engine

PR review, `fix-issue` acceptance and the final verification of an Owner batch run
through one engine: independent reviewer slots, then a synthesis that individually
challenges and consolidates their attributed findings. Workflow behavior stays in
adapters. PR review assesses a PR snapshot; issue acceptance assesses the local
candidate against the whole approved specification; Owner verification assesses the
combined diff against the selected feedback, every verification case and, when
present, the approved issue plan. The existing Owner and issue guards stay the
authority for acceptance (`owner_contracts.current`, `owner_commit.prepare`,
`owner_auto_commit.execute` and `owner_publication.automatic_proof`); the engine
only supplies the reviewer and synthesis pipeline.

**The candidate.** An Owner or issue candidate is an uncommitted tree that the Owner
checkpoint identifies. To reuse the commit-based review machinery, a deterministic
synthetic commit of that tree on the pinned baseline is created and pinned at
`refs/pr-owner/<session>/review-candidates/<commit>`. The same tree and baseline
always give the same commit, so a resume never loses evidence to a creation time;
another baseline gives another commit, ref and evidence. It is an internal review
artifact: never an implementation commit, never on the Owner branch, never in the
index and never pushed. Acceptance still binds to the checkpoint tree.

**Reviewer independence is input/session separation.** Each slot gets a fresh
disposable clone, private home, scratch directory and explicitly selected context
files. Peer reports, implementation sessions and Owner state are not supplied to
reviewers. Only synthesis receives both attributed reports, implementer checks as
untrusted claims and earlier rejected findings. These paths remain accessible to
processes inside the outer container: separate directories are not an access-control
boundary. Instructions prohibit inspecting peer or workflow artifacts.

| | Independent reviewer slot | Direct stage: single reviewer matching synthesis, and every synthesis |
| --- | --- | --- |
| Candidate | Disposable clone of the review commit | Fresh disposable checkout of the review commit |
| Session | Fresh session and private provider home | Fresh provider session, scratch `TMPDIR` |
| Security boundary | Enclosing container | Enclosing container |
| Retained guards | Filtered environment, read-only instructions, tool denials, snapshot verification | Filtered environment, review instructions, tool denials, candidate and Git-state verification |

Checkout mutations discard the result. Live-tree or Git-admin changes block
acceptance and retain the work for inspection. These are integrity checks, not
kernel prevention of writes. Failed workspace preparation stops the review rather
than running in the live candidate directory. New reviewer evidence uses the
`private-snapshot-container-v1` fingerprint policy; old evidence and its recorded
policy remain historical and cannot be reused as new-policy evidence.

**Disposable checkout.** Each direct stage gets its own checkout under its attempt
directory, outside the live Owner worktree and Git directory; its output and
`TMPDIR` are scratch there. Before and after execution the checkout's tracked content
must equal the candidate tree and its `HEAD` the review commit. The check uses a
temporary index filled from the Owner repository's Git directory, so a stage that
edits the checkout's index, configuration, `assume-unchanged` flags or filters cannot
hide an edit. Untracked and generated files are allowed and retained. A failed check
before a stage calls no model. After a stage the result is discarded, the phase is
`failed` with `failure_reason: candidate_checkout_modified`, the changed paths and
diff are kept, and the run stops with `review_checkout_modified`. The live candidate
is unchanged, so no `--adopt` is needed: resume reruns that stage in a fresh
checkout. The live-Owner integrity callback also runs before and after each stage;
its violation takes precedence and requires adoption. The checkout lacks ignored
files, installed dependencies and submodule contents: a stage may set up what it
needs in scratch, must report a case it cannot run as `unavailable` (or return
`incomplete` when material verification cannot finish), and never claims `tested`.

**Results.** The acceptance result has an `assessment` (`passed`, `changes_requested`,
`incomplete` or `needs_input`), `summary`, `checks`, attributed `findings`, one
evidenced outcome for every selected feedback or specification item, `case_results`
for every verification case and `questions`. `passed` allows no blocking finding,
clarifying outcome or question. The full result, including non-blocking observations
and rejected claims, stays in the engine artifact
`review-engine/<review commit>/<adapter>/reviews/synthesis.json`. Only findings with
`blocking: true` become the legacy proof: `harness_review.findings`, `harness_feedback`
and Owner `blocking`. A `passed` result maps to `status: ready` with empty legacy
findings and questions, so a candidate with observations passes the unchanged commit
guard. Observations never return to the implementation session as required work.

**Incomplete and unanswered results are retained, not reusable.** An `incomplete` or
`needs_input` result is written to its own per-attempt file
(`reviews/synthesis-incomplete-<n>.json`) with the phase status `incomplete` or
`needs_input`; it never replaces `synthesis.json` and an earlier attempt is not
overwritten. Resume reruns synthesis once on the unchanged candidate and keeps valid
reviewer reports; a result that is still incomplete stops again. Incomplete does not
count towards the repeated-rejection limit. An invalid synthesis result gets one
correction attempt on synthesis only.

**Evidence and reuse.** Evidence is bound to the exact candidate tree, baseline,
specification or feedback, and configuration, and is accepted on resume only after the
live tree is re-verified:

| Change | Stages rerun |
| --- | --- |
| Candidate tree or baseline | All |
| Specification or selected-feedback identity | All |
| One slot's provider, model or effort | That slot and synthesis |
| Synthesis choice, verification cases, implementer report or earlier findings | Synthesis only |
| An `incomplete` or `needs_input` earlier result, nothing else changed | Synthesis only |
| A direct stage's checkout check failed, or it was interrupted | That stage and later stages |
| Prompt, schema, validator or CLI version | The stages that use it |

Cancellation and runtime or quota limits keep every usable artifact, mark the stage
`interrupted`, set no acceptance and let resume rerun only what lacks valid
evidence. Engine stages are recorded in `batch['attempts']` with `kind:
review_engine` and their artifact paths. An orphaned engine stage never forces
`--adopt` while the tree and Git admin are unchanged; source changes still do.
Batches begun before the engine carry no `review_engine: 1` marker: an unfinished one
with attempts stops with `unsupported_session` and keeps its worktree and artifacts,
and a `ready` one can still commit and publish.

### Owner recovery and commit association

Owner sessions live under the state root's separate `owners-v1` subtree. Private
tree checkpoints preserve tracked and nonignored untracked files without touching
the real index or stash; ignored files are listed separately. Failed attempts,
plans, prompts, checks, and reports remain available. For partial/manual changes,
inspect `diff --current` and Magit before `resume --adopt`. Unstage any staged
changes first. `--replan` replans unfinished/changed feedback against accumulated
changes; `--skip TASK_ID` leaves dependent tasks blocked. Changed local or remote
HEAD during provider execution requires reconciliation and a new batch;
commit-only recovery recognizes its own already committed prefix. Starting a
new fix fast-forwards a clean owner worktree when it is behind the fetched PR
head, with progress messages showing both commits. Unfinished batches at that
HEAD, Git operations in progress, local changes, and genuinely diverged branches
require inspection in Magit. Ignored files are never overwritten by this update.
A new session automatically accepts tracked agent configuration unchanged from
the PR merge base. PR-introduced configuration, local or ignored additions, and
changes after that initial baseline return the exact diff and a token for explicit
`--trust-agent-config TOKEN`.

Skipping an unrelated task cannot authorize replay of an interrupted fixer.
Adopting partial work preserves the initial fix step for tasks that have not yet
started. A failed combined repair also requires explicit adoption before review
can continue. If selected feedback was deleted, skip its affected task, then replan
the remaining work; skipped tasks stay in history. A failed authentication probe
does not discard an otherwise unchanged, verified fix.

In Emacs, `f` or `C-u f` at the root heading selects only actionable Attention
items. Select another item or group explicitly to fix feedback outside Attention.
`K` also cancels an active fetch or publication operation; saved progress and
uncertain publication intents remain available for recovery. Repository selection
precedes toolkit selection when the checkout has multiple GitHub repositories.

Owner checkpoints cannot include uncommitted contents inside submodules. If one
is dirty, inspect and commit or reconcile it before continuing. The contents are
preserved. Ignored-file diagnostics show a bounded sample and total count.

Exact association recognizes either the whole verified patch with the baseline
as sole parent, or the recorded sequence of logical trees with matching parents.
Arbitrary splits/partial commits require explicit `associate --batch ID --item KEY --commit SHA`
(repeat items/commits as needed). Only verified code-fix outcomes get generated
fixed-in replies. Manual association generates them after their SHAs reach the
freshly fetched PR head; automatic completion stages them locally as Awaiting push.
Both require remote reachability and explicit confirmation for publication.
Ordinary composed replies start Unstaged. Changed discussion or loss of previously
confirmed remote commits unstages affected replies without replacing human wording.
Uncertain writes require `recover` before another send; confirmed receipts remain
historical after later amendments or force-pushes.

See the [Emacs owner keys](../emacs/README.md#pr-owner-toolkit),
[API contract](api.md#owner-toolkit), and
[interactive-fix roadmap](roadmap.md#interactive-owner-fixes). Interactive owner `F` remains a
separate future work package.

## Worktrees and updated snapshots

The CLI retains a detached worktree next to the main checkout, named
`<repository>-review-pr-<number>`. It uses private refs under `refs/pr-review/` and
does not update caller branches or remote-tracking refs. It holds an exclusive
repository/PR lock during generation. The lock is released before the terminal
menu, editor, or publication confirmation; mutations use short transactions.
When the worktree already exists, the CLI
automatically resumes the unique saved run matching its repository, PR number,
Git checkout, worktree path and checked-out commit. It prints the selected run and
retains the normal snapshot and worktree-integrity checks. Completed phases are
reused only when their inputs still match. If no run matches or several match,
it stops with guidance; `--resume` explicitly selects a run, including one stored
outside the current state directory. Existing worktrees are never overwritten.

Open PRs are checked against the current base branch. Closed or merged PRs use
the base commit recorded in GitHub's PR metadata, so later changes to the base
branch do not prevent a historical review. Publication remains disabled for them.

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

## Resume, generations, and history

Use `history`, `show GENERATION`, and `i ID` in the terminal to inspect earlier
human edits. `current` returns to the active generation, and `recover GENERATION ID`
creates a new Unstaged draft with the exact saved text and provenance. Its location
must be selected again. `docs` shows retained legacy combined bodies;
`recover-doc GENERATION NAME` recovers one as a separate Unstaged draft. Earlier
text is never automatically inserted into new drafts or given to models.

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
the candidate preserves unaffected slot reports and reruns only the invalid slots. Legacy
provider-named reports stay in place; they are copied to a slot only when their
original inputs and output can be verified. Unverifiable reports are retained
and the affected work is rerun.

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

## Saved artifacts

The initial session and its replacement attempts contain private artifacts. The CLI
prints the exact active artifact directory. Initial sessions live under
`${XDG_STATE_HOME:-~/.local/state}/pr-review/<host>/<owner>/<repo>/pr-<N>-<id>/`:

- `manifest.json`: identity, snapshot, tools, models, phase fingerprints and status.
- `context/`: PR metadata, paginated discussion, reviews, inline comments, CI, local diff.
- `discussion/`: versioned complete conversations and resolved/outdated thread metadata.
- `reviews/`: independent reports, raw Claude events, structured synthesis and final Markdown.
- `prompts/`: the rendered synthesis prompt, referencing explicit local artifacts.
- `logs/`: model events, errors, command arguments and integrity snapshots.
- `publication/`: contribution proposals, editable drafts, per-action status, exact
  submitted bodies, API responses, and archived drafts.

## Compatibility with older sessions

The backend reads old Bash ledgers and retains legacy combined bodies as
recoverable documents. Old automatic selections do not become staged comments.
Use the Python backend for a session once it has been adopted: an older Bash
binary does not understand the canonical draft ledger. Restoring local files does
not undo GitHub posts; retain intents and receipts and reconcile them before retrying.

## Trust, credentials, and GitHub access

Models receive filtered environments: credential-like variable names are removed,
except each CLI's own authentication variables. Only removed names are recorded.
Temporary Git config overrides (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_*`,
`GIT_CONFIG_VALUE_*` and `GIT_CONFIG_PARAMETERS`) are removed together, avoiding
both credential leakage and incomplete config entries that break Git discovery.
On-disk credentials remain accessible to the CLIs. Code and review context are sent
to the selected model providers. Use this tool only on repositories you trust.

The reviewer orchestrator does not run repository tests, builds or hooks.
Owner fixes can change source, and automatic owner commits honor hooks and signing.
Models may run investigative commands under their configured permissions.
After each invocation in the review worktree,
changes to HEAD, tracked files, the index or snapshot refs stop the run and block
publication. Generated untracked/ignored files are retained and disclosed.

CI includes Actions workflow runs and legacy commit statuses for the reviewed head.
Third-party GitHub App check runs are excluded. Empty results never mean CI passed;
unavailable endpoints are reported explicitly. Suggested fine-grained token
repository permissions are Metadata/Contents/Pull requests/Actions/Commit statuses
read, plus Pull requests write for publication. Read access does not verify write
permission; GitHub may reject the chosen review operation.

## Progress and troubleshooting

Slow review models are allowed to finish. Heartbeats say "Process running" and show
elapsed time and time since stdout or stderr changed; process liveness does not
prove progress. A warning appears after 60 seconds without output.
Synthesis and discussion matching stream Claude events, including partial messages,
so activity is visible before the final structured result. Raw events remain in
`logs/*.raw.json` files as newline-delimited JSON. Recognized terminal events
(including Codex's "not a git repository" error and `turn.failed`)
stop the model, escalating from TERM to KILL with bounded waits if needed. Other
error events are flagged while allowing retries. There is **no default timeout or
budget limit** for a silent review without a terminal event. Before each review
invocation, Git repository identity is checked in the model's filtered environment;
diagnostics are retained in `logs/<stage>.git-preflight.log[.stderr]`.
Ctrl-C forwards the signal to the model process group, escalates to TERM and KILL
with bounded waits if necessary, and records an interrupted stage. Successful phases are
reused only when their snapshot, inputs, model, version, policy and output hashes
still match. Invalidated independent reviews are archived and restarted as described
above. Local discussion refresh preserves human wording and unstages affected
comments for inspection. Running a new analysis or rematch creates a new generation
with fresh Unstaged drafts; earlier edits and staging remain in history. Legacy
version-1 runs remain readable but require a new run for discussion-aware publication.

| Situation | Next step |
| --- | --- |
| Authentication, quota, or model-access probe fails | Inspect the named probe logs, correct the CLI login or account access, then retry. Set `PR_REVIEW_CODEX_PROBE_MODEL` if the default probe model is unavailable. |
| Several saved runs match, or a worktree has no matching run | Use the printed guidance and `--resume /absolute/path/to/run` to select the intended session. Inspect existing worktrees before changing them. |
| Agent configuration changed | Inspect the exact configuration diff and explicitly trust that snapshot before running models. |
| A review fails or is interrupted | Rerun the same PR or use `--resume`; completed compatible stages are reused and the previous review remains available. |
| A publication response is uncertain | Use reviewer `resolve` or owner `recover` to read back GitHub state before another send. |
| An owner fix is paused | Inspect the verification blocker and current diff, then resume the selected batch. Use `--adopt` only after inspecting manual changes and unstaging them. |
| A model is silent | Check the activity timestamps and logs. There is no default model deadline; cancel with Ctrl-C or configure `PR_REVIEW_MODEL_TIMEOUT`. |

## Issue fixing

`bin/fix-issue` automates planning, implementation, pushes, and subsequent review
fixes. Its invocation authorizes these GitHub writes. It uses the existing Owner
fix engine, and the shared review engine for acceptance review of the finished candidate.
Emacs Owner commands retain
their explicit publication behavior.

Run from a checkout with a GitHub remote:

```sh
fix-issue -i 123 --reviewers codex
fix-issue -i 123 --approve 1
fix-issue -p 456
```

Install the entry point alongside `pr-review` (keep `bin`, `lib`, and `prompts`
together), or symlink `bin/fix-issue` into your PATH. Python and provider
requirements are the same as for `pr-review`.

### Eligibility and planning

The issue must be open and have a description. Both issue and PR must be
unassigned or assigned exclusively to the authenticated GitHub user. `-f` /
`--force` overrides only those assignment checks. Assignments are not changed.
There must be at most one open explicitly associated PR, in the same repository;
`-p` requires one linked issue. Forks and ambiguous relationships block execution.
On first adoption the PR branch may contain only empty single-parent commits.
Any source, documentation, configuration, or test changes prevent adoption.
A prior merged associated PR requires manual assessment.

The processing provider inspects a dedicated planning worktree and posts numbered
questions or a revisioned plan on the issue. Answer in issue comments; reference
question IDs or quote their wording. Missing details are clarified before coding.
Approve with an issue comment `/fix-issue approve REV` or pass `--approve REV`.
The latter records your explicit approval on GitHub and proceeds. Approval must
come from the GitHub user who started the workflow. Changed scope requires a new
plan revision. `--yolo` bypasses approval for this invocation, but never blocking
questions, assignment checks, or workspace/branch integrity checks.

### The specification

The approved plan is one canonical UTF-8 Markdown document per plan revision: the
complete specification followed by its acceptance criteria. The planner follows one
standard for every implementation provider, scaling detail with the size and risk of
the change rather than with the model that will implement it. A specification states:

- **Outcome** (required): observable behavior, with examples where they clarify.
- **Scope** (required): what is included and what is excluded.
- **Approach** (required): the existing components, patterns and constraints in this
  repository that the work builds on, and the integration boundaries.
- **Acceptance scenarios** (required): concrete scenarios, including regression cases.
- **Verification** (required): the tests and commands that demonstrate the result.
- Only when they apply: **Interfaces and defaults** (including precedence), **Errors
  and compatibility**, **Design decisions**, and **Assumptions and resolved choices**.

Small changes get a few lines under the required headings; a change across several
components resolves the decisions a reliable handoff needs. The standard forbids
padding, empty or placeholder sections, task checklists, and per-edit prescriptions
(line numbers, diffs). Technical design, decomposition, tests and repairs belong to
the implementation harness. Before returning a plan the planner must inspect the
existing behavior it relies on and list the consequential decisions. An unresolved
**product** choice (externally observable behavior, defaults, precedence, errors,
compatibility, scope) is never guessed: the planner asks numbered questions that
explain the alternatives and what each would change, and no plan is published until
they are answered. Never both a plan and questions.

The checks applied to a planner response are mechanical: well-formed fields, the
required sections present and non-empty, inspected paths that exist in the
repository, a plan that leaves no product choice unresolved, resolved decisions
recorded in the document, and no task ledger or diff. They cannot show that the
decisions are *sound*; that is what your approval and the independent acceptance
review are for. Read the published plan before approving it.

**Artifact lifecycle.** For each revision the coordinator writes the document once to
`issues-v1/<repo>/issue-N/specifications/revision-R.md` (read-only, UTF-8, outside
every worktree and Git directory) and records its digest and SHA-256 in the session.
The plan comment on the issue shows that exact document between the `## Plan
revision R` heading and the approval instructions; the revision label and the
`/fix-issue approve R` command are not part of the approved content. The approval
digest is derived from the document. A specification over GitHub's comment limit is
refused with `specification_too_large`; it is never truncated or summarized.
Unchanged discussion keeps the current revision and file; any content change is a new
revision that needs its own approval and keeps earlier revisions on disk.
On approval the file is copied, byte for byte, beside the Owner session
(`owners-v1/.../specifications/`). Before every implementation turn, on resumption,
and for acceptance review, the copy is verified against the approved digest; the
implementation provider (Codex or Claude Code) is handed the path of that Markdown
file, and the acceptance reviewer receives the identical text. Issue discussion and
the plan's provenance are passed separately and never amend the document. A missing
or altered file stops execution with `specification_missing` or
`specification_modified`, naming the file; restore the approved bytes from the plan
comment or approve a new revision. Plans saved before this lifecycle existed are
materialized from their exact approved `body` and acceptance criteria, unchanged. The
location of a file, or other metadata about it, never restarts an implementation
session; only a change of approved content does.

The standard, its mechanical validation and the artifact lifecycle are evaluated, with
their limits, in [the paired planning evaluation](evaluations/specification-planning/README.md)
(exploratory: it found no quality gain on its four tasks, a higher planning cost, and an
over-asking weakness in the first prompt that the current prompt addresses). Reproduce it with
`python3 docs/evaluations/specification-planning/evaluate.py prepare|verify-checks|run|summarize`;
`run` consumes provider quota and is never part of `make check-backend`.

When the base branch advances, the planner reassesses the existing specification.
If it remains valid, its revision and approval remain valid too; branch movement
alone does not publish another plan. Changed requirements still require a revised
specification and fresh approval. Feedback about implementation constraints,
verification, or review handoff deliverables belongs in that specification, even
when the feature scope is unchanged. Updates to the specification instructions
invalidate cached planning assessments on the next planning pass.

After approval, an empty commit and draft PR are created if needed. The PR body
links to the published plan comment and includes the intended issue-closing
reference; plan prose is not embedded, so it cannot add closing references. Bootstrap
and implementation commits honor configured hooks and signing. A server that
rejects an empty PR stops bootstrap; the program never adds placeholder files.
If the fixer subsequently determines no code is needed, the draft remains for
manual closure. Planning can also finish with evidence that no changes are
needed before creating a PR.

### Execution, settings, and recovery

Implementation uses Owner's isolated worktree. Main-checkout edits do not affect
it; any linked worktree in the same underlying Git repository resumes the same
local issue session. State is stored in `issues-v1` below the normal state root,
with execution state in the linked `owners-v1` session. Resume from another
machine or without those local records is not supported.

The entire approved specification is delegated to one persistent implementation
session. The harness handles its own decomposition, coding, testing and debugging.
The shared [review engine](#shared-review-engine) reviews a completed local candidate;
only blocking findings go back to the implementation session. There are no per-task
repair credits or intermediate user checkpoints. Product questions are published on GitHub; internal
design and verification problems remain implementation work.

Recorded partial work and provider session identity survive interruption. Source
or Git changes not accounted for in saved checkpoints require inspection and
`--adopt` before continuation. Unfinished sessions from the retired task pipeline
are no longer automatically migrated; their worktrees and artifacts remain available
for manual recovery.

Resume unfinished implementation with the normal command. The former issue
`--replan` option and repair-credit authorization have been removed. Existing
attempts and plans remain readable history. Interactive Owner retains its own
explicit replanning controls.

Shared reviewer flags and economical/expensive presets use normal precedence:
defaults, preset, saved settings, then explicit individual flags. They configure the
acceptance review: `--reviewers`, `--reviewer-N-provider/model/effort`,
`--processing-*`, `--claude-*`, `--codex-*` and `--synthesis-tests` set the Owner
`final_review`, under the same frozen-settings checks. Without them a settings file's
`final_review` applies, then one slot derived from its `review`, then the common
defaults; a new session without a file starts from those defaults and a session
without `final_review` stays single-reviewer. `--settings-file` accepts the Owner JSON
shape; `review` configures interactive task checks only:

```json
{
  "fix": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
  "review": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
  "final_review": {
    "reviewer_slots": [
      {"id": "reviewer-1", "provider": "claude", "model": "opus", "effort": "high"},
      {"id": "reviewer-2", "provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}
    ],
    "synthesis": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
    "synthesis_tests": "focused"
  },
  "verification": "focused"
}
```

Started batches retain their settings. Agent configuration changes require the
normal `--trust-agent-config TOKEN` approval, reported with the blocker details.

After the engine passes the candidate, the coordinator commits, pushes, and marks
the PR ready. Blocking findings return to the same implementation session before
publication; there is no separate post-push review loop. An incomplete review stops
with `review_incomplete`, keeping the review phase so resume reruns only the stages
without valid evidence; `independence_failed` and `review_checkout_modified` also
stop with a message (the latter needs no adoption). Verified
replies are published, while human thread resolution remains manual. The saved
Owner `verification` setting remains compatible with interactive Owner; its focused
testing policy does not constrain whole-issue implementation.

Unexpected remote commits stop execution even between batches. Neither `--force`
nor `--yolo` authorizes them. Interrupted model edits remain in the Owner worktree;
inspect them before using `--adopt` to accept them for reverification. Adoption
does not accept remote commits or approve expanded scope. Hooks, signing,
permissions, and branch rules can stop commits or pushes; fix the reported cause
and invoke again. Publication intents and read-back receipts recover uncertain
writes without blindly reposting.

If a comment or draft-PR request loses its response, ordinary retries first
read back outstanding intents before assessing the discussion. An exact accepted
write is recovered without sending it again. If it is absent, inspect GitHub and
the local journal with:

```sh
fix-issue -i 123 --inspect-writes --json
fix-issue -i 123 --retry-absent 'plan:1'
fix-issue -i 123
```

Use the exact action key shown by inspection (`create-pr` for PR creation).
`--retry-absent ACTION` is your explicit decision that the uncertain request
stored nothing remotely. It performs another read-back and changes only that
action to pending, retaining its previous intent and the decision in history;
it does not send anything. The next normal invocation may retry it. If the write
is now found, its receipt is recovered instead. Multiple matches, changed
publications, a saved receipt whose publication disappeared, or a failed read
block this reset. Inspect those cases manually; neither ordinary retries nor
`--force`/`--yolo` authorize duplicate sends.

`--json` returns eligibility checks, phase, URLs, session/worktree paths, and next
action; progress goes to stderr. Exit codes are 0 for success/no-op, 1 for failure,
2 for waiting on answers/approval, 3 for attention, 4 for partial publication,
5 for uncertain remote outcomes, and 130 for interruption.

### Behavioral verification in Owner

Owner planning and fixing also record a bounded set of behavioral verification
cases. Compatibility work should distinguish explicit choices from defaults and
saved formats from new formats, using the approved requirements and baseline as
evidence. Repairs retain those cases and check related regressions; independent
review checks every case and looks for omissions. Failed or unavailable evidence
cannot support successful verification. This does not add model calls or increase
the repair allowance. Older saved plans remain usable.

Case evidence is tied to the source reviewed. Task commits remain separate from
any integration repair, and the final combined tree must pass review before
publication. Test claims in reports are evidence for the reviewer to check, not
commands automatically executed by the coordinator.

### Shared eligibility services

`pr_review.issue_eligibility` separates fact collection from pure evaluation.
`collect_eligibility_facts` accepts complete prefetched facts for scanners;
`evaluate_eligibility` returns eligible/ineligible/indeterminate, workflow
disposition, individual reason codes, and an advisory next action. Collection
may fetch dedicated `refs/fix-issue/` snapshots but never changes working files
or publishes. Unknown facts never pass. Semantic intake is a separate provider
operation. A future scanner can add its own label filter and reuse these checks;
execution always refreshes eligibility before writes.

## Autonomous issue execution and resource controls

After approval, one persistent Codex or Claude Code implementation session owns
the whole specification: exploration, decomposition, coding, test setup, tests and
debugging. The coordinator does not split it into independently launched tasks,
generate test contracts, or replay reported test commands. It resumes the exact
implementation session when more work is needed; it never selects a global “last”
session. Interactive Owner keeps its existing task workflow and focused policy.

When the harness reports a complete candidate, the shared review engine assesses the
whole approved scope and the source tree: one or two independent reviewers, then a
synthesis that challenges and consolidates their findings. Blocking findings go back
to the same implementation session; non-blocking observations stay in the engine
artifacts. Reviewers inspect existing evidence and run additional checks where
needed, rather than automatically repeating the entire test suite. The engine is the
sole acceptance gate. Its reviewers and synthesis come from the Owner `final_review`
settings, which the shared reviewer flags configure; they do not start post-push
reviews. Use `bin/pr-review` separately when an additional PR review is wanted.

The coordinator retains approval and eligibility checks, isolated worktrees,
source/Git integrity checks, exact final-tree verification, journaled commits and
pushes, and GitHub questions/status messages. Completed commit boundaries remain
recorded; new work uses one whole-specification execution. New executions normally produce one verified commit
per candidate, without model-generated task commits. Final verification is required
before any implementation claim can be published.

Real product choices are asked on GitHub. Repeated returns without source progress
end with a diagnostic; provider failures and resource limits preserve partial work
and the exact implementation session. Recorded interrupted changes can resume;
unrecorded external edits still require explicit inspection/adoption. A process
exit is not a routine implementation checkpoint or a scheduling requirement.

`--max-runtime 30m` (or `2h`, `90s`) limits cumulative active time after approval,
including provider calls, checks and automatic recovery. Invocation gaps and
approval waits do not consume it. Saved limits remain in effect unless explicitly
replaced; increasing `--max-runtime` increases the total allowance, not just the
next invocation. Runtime expiry cancels owned subprocesses and returns
`resource_limit`; interrupted work is unverified.

`--min-weekly-quota-remaining 20` preserves an observed 20% weekly reserve.
`--max-weekly-quota-use 5` stops after an observed five-percentage-point increase
during active execution. Both may be used together and apply independently to
provider allowance pools. Observations may include simultaneous account activity.
Resuming does not replenish consumed allowance. Reset/telemetry gaps are reported
as unknown, never zero; configured runtime provides the fallback. A requested
quota guard without available telemetry or an explicit runtime fallback stops
before unmonitored model work. Codex weekly telemetry uses local app-server
account/rateLimits/read without model turns; Claude weekly telemetry is presently
unavailable in this adapter. No private endpoint or extracted credential is used.

Quota polling is at most once per minute. Provider reporting delays and requests
already in flight can overshoot a threshold. Tokens are not converted into quota
percentages. Neither percentage monitoring nor a runtime limit guarantees an exact
subscription spend. With `--json`, human progress stays on stderr and stdout has
one final result, including a versioned execution/resource summary.
