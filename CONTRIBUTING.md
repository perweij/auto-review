# Contributing to Auto-review

Report bugs and propose focused changes through this repository's issues and pull
requests. For a bug report, include the command or Emacs action, expected and actual
behavior, tool versions, and a minimal reproduction. Saved session artifacts can
contain source, private discussions, and provider output; inspect and redact any
excerpts before sharing them.

## Development setup

Use a source checkout as described in the [README](README.md#install).
The production backend requires Linux and Python 3.9+ and has no pip dependencies.
Backend checks additionally require Git, Bash, Bats 1.5+, ShellCheck, and jq.
The deterministic suite mocks GitHub and model CLIs; credentials are not required.

Frontend checks require Emacs 29.1+, Magit 4.6+, Transient 0.13+, and Forge on the
Emacs load path. `EMACS` selects the executable. `AUTO_REVIEW_TEST_LOAD_FILE` may
name an Emacs Lisp file that configures those dependencies for a test run.
Both compilation and ERT disable site startup with `--no-site-file`; dependencies
registered only by a site-start file must be supplied explicitly. Environment/profile
load paths remain available. For example, a dependency setup file can add the
directories for Magit, Forge, Transient and their dependencies:

```elisp
(setq load-path (append '("/path/to/magit/lisp"
                          "/path/to/forge/lisp"
                          ;; Add Transient and the remaining dependency directories.
                          ) load-path))
```

Run it with `AUTO_REVIEW_TEST_LOAD_FILE=/absolute/path/to/dependencies.el make check-emacs`.
The setup file must load only third-party dependencies, not an installed auto-review
package or its autoloads. The runner restores checkout load-path precedence and
checks module and function origins in both processes. A preloaded foreign copy
fails with `Auto-review test contamination` rather than silently testing mixed code.

## Architecture

- `bin/pr-review` resolves its real path and loads the standard-library Python
  backend from `lib/pr_review/`. Keep the installation's relative layout intact.
- CLI and JSON API adapters call shared application services. Repository/context
  collection, provider execution, saved generations, drafts, and publication live
  in the backend. See the [protocol reference](docs/api.md).
- PR review, `fix-issue` acceptance and Owner final verification share one review
  engine (`review_engine.py`: independent slots, then synthesis). Adapters supply
  their prompts, schema, validator and snapshot integrity checks; the Owner and
  issue guards stay the authority for acceptance.
- Reviewer and Owner have separate orchestration and saved sessions. Owner state
  lives under `owners-v1`; it must not appear as reviewer history. Shared services
  handle GitHub access, process supervision, storage, and publication recovery.
- `emacs/` owns Magit sections, Transient menus, editing, and presentation. It uses
  the versioned API rather than reading private backend state files directly.
- `prompts/` contains runtime prompts, JSON schemas, and GraphQL queries. Prompt
  and schema changes can invalidate cached review stages.

Preserve human edits, generation history, publication intents, and receipts when
changing persistence. Reviewer integrity checks must remain independent of Owner's
ability to modify its worktree. Publication previews must show the exact text and
destinations; models must not publish, push, or resolve threads implicitly.

When locks are needed together, acquire the owner execution lock, then the shared
PR lock, then owner state/draft locks. Do not hold state/publication locks across
model execution, editor sessions, or confirmation prompts. Revalidate revisions
when accepting changes, and preserve external Git/Magit edits on conflicts.

## Checks

Run from the repository root:

```sh
make check-backend  # Shell checks, Bats integration tests, Python unit tests
make check-emacs    # Warning-free byte compilation and ERT tests
make check-all      # Backend and frontend
```

`make check` is an alias for the backend checks. Tests exercise real temporary Git
repositories and worktrees with mocked providers/GitHub. They cover authentication,
snapshots, recovery, process cancellation, cache reuse, owner fixes, and exact
publication. Legacy session fixtures remain part of compatibility coverage.

Checks can read from a read-only source checkout when writable temporary storage
and the documented dependencies are available. Python lint compiles syntax in
memory, imports run with bytecode caching disabled through Make, and Emacs byte
compilation uses a temporary directory removed at exit. The Git fixtures and
compiled Emacs files still require writable scratch; set `TMPDIR` to an existing
permitted directory if the default temporary storage is unavailable. Set
`AUTO_REVIEW_TEST_LOAD_FILE` when Magit/Forge are installed outside the default
Emacs load path or normally supplied by site startup. Runner-isolation regression
tests use small Lisp fixtures and real Emacs without Magit/Forge; they are skipped
in backend-only environments without a supported Emacs executable.
A completely read-only model sandbox cannot run the full suite
merely by disabling Python bytecode. Do not relax its restrictions: report the
missing permission/dependency and continue code assessment or use supplied results
for the exact snapshot. A failed suite launcher does not mean its tests executed.

For Guix's installed-package checks, supply your own channels file:

```sh
make check-guix GUIX_CHANNELS=/path/to/channels.scm
```

See [packaging](docs/packaging.md) for the runtime boundary and build requirements.
Local checks do not establish that a Guix closure builds or that the suite executes
on the minimum Python version. Report unavailable checks explicitly.

## Optional live checks

These commands use authenticated model CLIs and incur provider usage. They use
synthetic fixtures and do not publish to GitHub. Run them deliberately when
validating provider integration, especially after CLI upgrades:

```sh
tests/smoke.sh
python3 tests/smoke-owner.py --provider claude --model haiku --effort low
python3 tests/smoke-owner.py --provider codex --model gpt-6-luna --effort low
python3 tests/smoke-engine.py --provider claude --model haiku --effort low
python3 tests/smoke-engine.py --provider codex --model gpt-6-luna --effort low
tests/evaluate-contributions.sh
tests/replay-pr4.sh          # one full review of the frozen PR4 corpus through the shared engine
```

The reviewer smoke test retains artifacts under `/tmp`. Model availability and
semantic results depend on the provider; passing mocked tests or CLI help checks
does not establish live integration or guarantee model judgment.

## Pull requests

Describe the problem, resulting behavior, and checks run. Add regression coverage
for behavior changes, and update the manual, Emacs reference, or API contract when
the corresponding interface changes. Keep future features in the
[roadmap](docs/roadmap.md), separate from implemented behavior. Avoid committing
local state, credentials, compiled caches, or machine-specific setup.


Provider execution relies on the enclosing sandbox container. Tests should verify
separate reviewer inputs, checkouts and sessions, plus integrity and publication
guards. Do not assert that peer artifacts are inaccessible at the OS level.
Claude command builders must retain the per-invocation sandbox-off settings;
Codex execution uses full access with approval prompting disabled. Test both fresh
and resumed commands without changing global provider or container configuration.
