# Auto-review

Review GitHub pull requests and work through incoming feedback from Emacs.
Auto-review brings Claude and Codex into Magit, with local worktrees, editable
review drafts, and explicit confirmation before publishing to GitHub.
A standalone command-line interface uses the same backend and saved reviews.

## What it does

- **Reviewer:** run Claude, Codex, or both against the same Git snapshot. With
  both enabled, one provider challenges and consolidates their findings.
- **Discussion-aware drafts:** compare findings with existing conversations,
  inspect them in code context, edit the wording, and stage individual comments.
- **Owner:** turn incoming feedback or local findings into verified fixes in a
  dedicated worktree. Prepare logical commits and replies, with optional
  automatic committing. Pushing and publication remain explicit actions.
- **Resumable work:** retain reports, human edits, and publication receipts;
  resume interrupted runs and inspect earlier generations.
- **Follow-up:** check selected review conversations against updated code without
  running another full PR review.

## Requirements

- Linux, Python 3.9+, Git, and the GitHub CLI (`gh`).
- A user-managed sandbox container as the execution security boundary.
- Authenticated Claude Code and/or Codex CLI installations for the providers you
  enable. New reviews default to both. Claude requires its `/code-review` skill
  and support for `auto` permission mode.
- For Emacs: Emacs 29.1+, Magit 4.6+, and Transient 0.13+.
  Forge is optional and adds pull-request menu integration.

The backend uses the Python standard library; no pip installation is needed.
Model runs and live authentication probes consume provider usage. Code and review
context are sent to the selected providers.

Use trusted repositories in a container sandbox, such as Podman. Codex runs with
`sandbox_mode=danger-full-access` and `approval_policy=never`. Independent
reviewers use separate disposable checkouts and fresh provider homes. Claude
launches explicitly set `sandbox.enabled=false`; neither the application nor its
provider execution settings add a nested OS sandbox. Reviewer independence is
input/session separation, not kernel-enforced peer secrecy. Project agent
settings load normally, and changes to agent configuration require explicit
trust. See the [trust and credential details](docs/manual.md#trust-credentials-and-github-access).

## Install

Clone the repository and keep its directory layout intact:

```sh
git clone https://github.com/perweij/auto-review.git
cd auto-review
mkdir -p ~/.local/bin
ln -s "$(pwd)/bin/pr-review" ~/.local/bin/pr-review
```

Ensure `~/.local/bin` is on your `PATH`. Authenticate `gh` and each enabled model
CLI using `gh auth login`, `claude auth login`, and/or `codex login`.
For HTTPS Git authentication through `GH_TOKEN`, configure `gh auth setup-git`.

Add this to your Emacs configuration, replacing the paths with your checkout:

```elisp
(add-to-list 'load-path "/path/to/auto-review/emacs")
(setq magit-auto-review-executable "/path/to/auto-review/bin/pr-review")
(require 'magit-auto-review)
```

A [Guix package](docs/packaging.md) installs the backend and frontend together.

## Get started in Emacs

1. Open a Git repository and run `M-x magit-auto-review`.
2. Press `g` in the PR list to discover open pull requests on GitHub.
3. Select a PR and press `R`, configure the providers, then press `r` to review.
4. Inspect findings in context. Use `e` to edit, `s` to stage, and `u` to unstage.
5. Press `P` to preview the staged comments and explicitly confirm publication.

Opening a buffer reads saved local data; it does not run models. Opening from a
Forge PR goes directly to that PR. Magit dispatch uses `C-c r`; Forge menus use
`V`. The `?` menu shows available actions.

Press `M` to switch between **Reviewer** and **Owner**; the choice is remembered
per repository. In Reviewer, `o` sends selected local findings to Owner for fixing.
In Owner, `G` refreshes GitHub feedback and `f` fixes the selected item or group.
Inspect the result before committing and pushing. `C-u f` opts into automatic
commits and staged replies; it never pushes or publishes automatically.

See the [Emacs reference](emacs/README.md) for the full workflow, keybindings,
commit preparation, and recovery commands.

## Use from the terminal

Run from a checkout of the repository you want to review:

```sh
pr-review 123
pr-review 123 --reviewers codex-1
pr-review 123 --reviewers claude-1,claude-2 --processing claude-3
pr-review 123 --reviewers codex-1,codex-2 --processing codex-3
pr-review https://github.com/org/repository/pull/123 --no-publish
pr-review 123 --resume /absolute/path/to/run
pr-review drafts /absolute/path/to/run
```

The interactive menu lets you inspect, edit, and stage drafts before publication.
Fresh proposals start Unstaged; publication requires a preview and final `yes`.
`--no-publish` generates and saves the review without opening that menu.

For explicit unattended review and publication:

```sh
pr-review 123 --auto-publish --reviewers codex-1 --processing codex-1 --codex-1-model gpt-6-luna --codex-1-effort medium --json
```

Automatic mode posts COMMENT feedback only. If any finding needs judgment or
correction, the whole batch stops before posting; analysis and drafts remain
available in Emacs with reasons and recovery actions. No new findings means no
GitHub post. The JSON result and exit status distinguish completion, attention,
partial publication, and uncertainty. See [automatic reviews](docs/manual.md#automatic-reviews).

Add `--suggested-changes` to opt into independently verified GitHub code
suggestions for small, self-contained fixes. In Emacs reviewer setup, `R s`
toggles suggestions and `R P` starts an automatic COMMENT review. See
[suggested changes](docs/manual.md#suggested-changes) for inspection and recovery.

Incoming feedback and fixes use `pr-review owner`; see the
[owner workflow](docs/manual.md#pr-owner-workflow). Run `pr-review --help` or
`pr-review owner --help` for command options.

## Documentation

| Guide | Contents |
| --- | --- |
| [User manual](docs/manual.md) | Configuration, CLI workflows, saved state, recovery, and safeguards |
| [CLI cookbook](docs/cookbook.md) | Reviewer combinations, model/effort settings, single-provider operation, and Owner examples |
| [Emacs reference](emacs/README.md) | Magit/Forge setup, keys, review and owner buffers |
| [GitHub workflow](docs/github-review-process-overview.md) | Findings, conversations, verdicts, and merge requirements |
| [Guix packaging](docs/packaging.md) | Installation, reusable recipes, and package validation |
| [API reference](docs/api.md) | Versioned JSON protocol for frontends |
| [Roadmap](docs/roadmap.md) | Planned features and outstanding validation |

## Contributing

Bug reports and focused pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the architecture, development dependencies,
and test commands. `make check-backend` runs the deterministic backend checks;
`make check-all` also checks the Emacs frontend.

## License

GNU General Public License, version 3 or later (`GPL-3.0-or-later`).
See [LICENSE](LICENSE) for the full text.

## Job usage reports

`fix-issue` and `pr-review` report usage automatically after model calls and print
cumulative tables when an operation finishes, pauses or fails. One table shows
weekly provider meters (including scoped Claude meters such as Fable); the other
shows role, provider, configured model, effort, calls, reported tokens and observed
weekly percentage points. The tables also appear in existing Emacs progress logs.

Accounting follows the saved job through resumes and review-generation changes.
Planning, live authentication probes, retries, implementation, review, synthesis
and optional model operations are included; cached results add no new calls.
Changing a role's model or effort creates a separate row. Older jobs track forward
from their next invocation and identify earlier usage as unknown.

Quota reports read Codex's local account interface and Claude Code's structured
`/usage` report without model inference. Availability depends on the installed CLI
and credentials. These reports do not expand the existing quota stopping controls.
Provider percentages are account-wide observations: other activity, delayed updates,
rounding and weekly resets can affect attribution. Role estimates may differ from
provider totals; overlapping meters are not additive. Missing readings and partial
interrupted token usage are marked explicitly, not treated as zero. Tokens and
estimated API costs are never converted into subscription percentages.

CLI JSON results and session reads expose an additive versioned `usage` summary.
See the [API reference](docs/api.md#usage-reporting) for coverage and details.

## Fix an issue from the terminal

`fix-issue` plans on the GitHub issue, then delegates the approved specification to a
persistent Codex or Claude Code session, with guarded commits, pushes and review:

```sh
bin/fix-issue -i 123
bin/fix-issue -i 123 --approve 1
bin/fix-issue -p 456
```

The approved plan is one Markdown specification document (outcome, scope, repository-grounded
approach, acceptance scenarios and verification, plus interfaces, defaults, errors and decisions where
they apply). The comment you approve shows that exact document, and the same file is handed to the
implementation provider. See [The specification](docs/manual.md#the-specification).

Repeat the command after answering questions or receiving review feedback.
Questions and revisioned plans remain on the issue. A draft PR is bootstrapped
with an empty commit after approval; implementation runs in an isolated Owner
worktree. Successful independent harness verification marks the PR ready after a guarded push.

### Autonomous issue execution

After approval, one implementation harness owns planning the code changes, coding,
testing and debugging until the entire specification is ready. The shared review
engine (the same independent reviewers and synthesis as `pr-review`) checks the candidate;
blocking findings return to the same implementation session. New batches preserve
explicit finding lineage and earlier decisions in a versioned ledger. Synthesis
sees that history, the latest implementation report, changes since the previous
reviewed candidate and references to immutable test-evidence bundles. Independent
reviewers inspect the candidate without that history. Reopened closed blockers,
or a blocker surviving two completed repairs, receive one focused adjudication.
After an adjudication, persistence requires two further completed repairs before
another adjudication; a reopened closed concern still triggers immediately.
Existing no-progress safeguards take precedence after review establishes a third
unchanged rejection, stopping before adjudication. A new report or evidence can
still establish acceptance of unchanged source.
The coordinator handles safeguards and GitHub communication. There is no second
post-push PR-review pipeline. It does not launch a new coding agent for each task or replay test suites.
Real product questions still require an answer. Interactive Owner retains its
existing task workflow and focused verification policy.

Progress goes to stderr; `--json` emits one final result on stdout. Optional limits:

```sh
bin/fix-issue -i 10 --max-repair-cycles 5 --json
bin/fix-issue -i 10 --retry-review-adjudication --json
bin/fix-issue -i 10 --max-runtime 2h --json
bin/fix-issue -i 10 --min-weekly-quota-remaining 20 --max-weekly-quota-use 5 --max-runtime 2h --json
```

New batches allow three repair cycles by default. Initial implementation/review,
format corrections and interrupted continuations do not each consume a cycle.
`--max-repair-cycles` accepts a positive integer; omission on resume preserves the
saved allowance. Increasing it retains approval, completed reviews and consumed
cycles. Lowering it preserves consumed cycles and finishes an already allocated
cycle, then prevents further allocations above the new allowance. Legacy batches
retain their existing behavior and reject these new review controls.

An unresolved adjudication stops with `review_conflict`; unchanged ordinary resume
makes no model or authentication calls. `--retry-review-adjudication` authorizes
one more focused adjudication of the saved current conflict. That invocation runs
only adjudication: if blocking findings remain after a resolved decision, it
returns `needs_attention` with `next_action: resume_repair`; resume normally to
start repair from that saved decision. A complete acceptance can proceed through the existing
commit/publication guards. Changed source, requirements, trust or evidence require
normal review. Rejection of findings cannot supply missing acceptance outcomes,
answer questions or erase verification limitations.

A repair-limit or review-conflict stop retains unaccepted code locally and posts
a deduplicated status with cycles, stable finding IDs, worktree/artifact locations
and a resume command. It does not push the checkpoint or mark the PR ready. Keep
that local worktree and its session artifacts available for recovery; draft
checkpoint publication and external evidence imports are separate work.
Implementation turns and repair cycles are reported separately. JSON results
include execution stage, cycle counts and stop reason. Adjudication consumes the
same runtime/quota allowance as other model work.

Runtime is cumulative active execution across resumes. Weekly usage is an
account-wide observation in percentage points, not a token-derived estimate or
exact attribution to this job. Codex quota monitoring uses its supported local
account interface. Claude weekly monitoring currently uses the explicitly supplied
runtime fallback. Delayed telemetry and concurrent account activity can affect
quota observations; a time limit does not guarantee a particular quota spend.
