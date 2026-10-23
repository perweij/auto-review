# pr-review

A local GitHub PR review CLI: Claude reviews first, Codex reviews the
same Git snapshot independently, then a separate Claude invocation challenges and
consolidates both reports. A final Claude pass compares findings with the latest
discussion and drafts useful contributions. GitHub publication requires a
human-selected action and an explicit final confirmation.

## Install

Keep this directory intact and put `bin/pr-review` on your PATH, for example:

```bash
mkdir -p ~/.local/bin
ln -s "$(pwd)/bin/pr-review" ~/.local/bin/pr-review
```

Requires Linux, Bash 4.4+, Git, `gh`, `codex`, `claude`, `jq`, `flock`, `timeout`,
and standard GNU utilities. The CLI checks capabilities through help output before
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
pr-review 123 --resume /absolute/path/to/run
```

Repository discovery uses `gh repo view`; specify `--repo` when it is ambiguous.
Exactly one Git remote must match the target repository. Fork PRs are fetched
through the base repository's `refs/pull/<N>/head`, without adding a fork remote.
The default models are `gpt-6-astra` and `fable`, both at high effort.

The CLI retains a detached worktree next to the main checkout, named
`<repository>-review-pr-<number>`. It uses private refs under `refs/pr-review/` and
does not update caller branches or remote-tracking refs. It holds an exclusive
repository/PR lock throughout the run. Existing worktrees are never overwritten;
reuse one with its original `--resume` directory. For a new snapshot, first inspect
and manually remove or move the old review worktree using `git worktree`.

Artifacts are private files under
`${XDG_STATE_HOME:-~/.local/state}/pr-review/<host>/<owner>/<repo>/pr-<N>-<time>-<id>/`:

- `manifest.json`: identity, snapshot, tools, models, phase fingerprints and status.
- `context/`: PR metadata, paginated discussion, reviews, inline comments, CI, local diff.
- `discussion/`: versioned complete conversations and resolved/outdated thread metadata.
- `reviews/`: independent reports, raw Claude events, structured synthesis and final Markdown.
- `prompts/`: the rendered synthesis prompt, referencing explicit local artifacts.
- `logs/`: model events, errors, command arguments and integrity snapshots.
- `publication/`: contribution proposals, editable drafts, per-action status, exact
  submitted bodies, API responses, and archived drafts.

Slow models are allowed to finish. Heartbeats show elapsed time and time since
output; there is **no default timeout or budget limit**. Ctrl-C forwards the signal
to the model process group and records an interrupted stage. Successful phases are
reused only when their snapshot, inputs, model, version, policy and output hashes
still match. A changed Claude independent input after Codex has run requires a new
run, to preserve independence. Unchanged contribution proposals retain human edits
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

The final screen groups findings by what they add to the conversation:

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
or review bodies go in the main review with links. The tool never resolves or
reopens threads. An outdated comment is not automatically a resolved issue.

Use `t p1` to toggle a proposal, `i p1` to inspect its full discussion, `e p1` to
edit its draft, `s` to inspect suppressed duplicates, `v` to preview the batch, and
`f` to display the full local review. The default screen stays compact.
Plain `e` edits the main review body. `p` publishes selected thread replies only;
`c` publishes selected contributions with a COMMENT review when needed. Replies
alone do not generate a redundant summary. Nothing new selected means no COMMENT
is posted. Explicit APPROVE or REQUEST_CHANGES verdicts remain available.

The menu defaults to not publishing; self-authored PRs offer COMMENT only.
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
On-disk credentials remain accessible to the CLIs. Code and review context are sent
to the selected model providers. Use this tool only on repositories you trust.

The orchestrator does not run repository tests, builds or hooks. Models may run
investigative commands under their configured permissions. After every invocation,
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
