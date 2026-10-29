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
- Authenticated Claude Code and/or Codex CLI installations for the providers you
  enable. New reviews default to both. Claude requires its `/code-review` skill
  and support for `auto` permission mode.
- For Emacs: Emacs 29.1+, Magit 4.6+, and Transient 0.13+.
  Forge is optional and adds pull-request menu integration.

The backend uses the Python standard library; no pip installation is needed.
Model runs and live authentication probes consume provider usage. Code and review
context are sent to the selected providers.

Use trusted repositories in a container sandbox, such as Podman. Project agent
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
pr-review 123 --reviewers codex
pr-review https://github.com/org/repository/pull/123 --no-publish
pr-review 123 --resume /absolute/path/to/run
pr-review drafts /absolute/path/to/run
```

The interactive menu lets you inspect, edit, and stage drafts before publication.
Fresh proposals start Unstaged; publication requires a preview and final `yes`.
`--no-publish` generates and saves the review without opening that menu.

For explicit unattended review and publication:

```sh
pr-review 123 --auto-publish --reviewers codex --codex-model gpt-6-luna --codex-effort medium --json
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
