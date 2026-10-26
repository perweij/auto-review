# Guix packaging

Auto-review owns the recipes for its Python backend and Emacs frontend. The
consumer supplies the source and version, and evaluates them using its own
pinned Guix channels. Both halves use the same source and version. No pip or
Emacs package-manager installation runs at startup.

## Local development

The root `guix.scm` returns the Emacs package, which propagates its backend,
Magit and Forge. It snapshots the local code, prompts, packaging files and tests;
root-level files outside that allowlist, hidden files, symlinks, state and
compiled caches are excluded. Review the included directories before building:
the selector is not a secret scanner. Edits require a rebuild and cannot change
an already installed generation.

From this checkout, using the adjacent stable-emacs pin:

```sh
guix time-machine -C ../stable-emacs/channels.scm -- build -f guix.scm
guix time-machine -C ../stable-emacs/channels.scm -- shell -f guix.scm emacs
make check-guix GUIX_CHANNELS=../stable-emacs/channels.scm
```

Any suitable caller-owned channels file can replace that path. `GUIX` selects
the Guix executable for `make check-guix`. Auto-review does not maintain an
independent Emacs/Magit/Transient pin.

The existing stable-emacs development overlay can still load this `guix.scm`.
The normal stable-emacs manifest and Fleet installation do not yet include
auto-review; that integration is a separate step.

## Reusable recipe interface

Add this repository's `guix` directory to Guile's load path to import
`(auto-review packages)`. The module exports:

- `(make-auto-review-backend source #:version version)`: returns the backend
  package. Version defaults to `"0.1.0"` for development. Source must produce a
  directory with `bin`, `lib`, `prompts`, `emacs` and test fixtures, as a
  recursive `local-file` or a `git-fetch` origin does.
- `(make-emacs-magit-auto-review backend)`: returns the frontend, deriving its
  source and version from that backend and propagating that exact package.

The module selects no source URL, revision, channel or credential. Its
constructors use the consuming Guix graph. Construct a new pair when changing
source; do not override only the source field of an already constructed package,
because the backend builder captures the constructor's source argument.

For the later stable-emacs release integration, stable-emacs will carry an exact
copy of `guix/auto-review/packages.scm` on its channel load path, recording the
upstream revision. It will supply a public Git source pinned by commit and hash
from that same revision. The recipe must be available during Guix evaluation;
a source fetched during a package build cannot itself supply the recipe for
that evaluation. This arrangement needs no auto-review channel or Fleet
component. Keep the copied module and source pin together when updating.

## Runtime boundary

The backend contains `bin/pr-review`, `lib/pr_review` and `prompts` with their
relative layout preserved. It resolves symlinks to find these resources and
does not write Python import caches. Its shebang names its Guix Python directly.

Packaged Git, GitHub CLI (Guix package `github-cli`, executable `gh`) and SSH
precede the inherited PATH only inside the backend and its children. They are
ordinary runtime inputs, so installing auto-review does not independently add
those commands to the user's profile. `SSL_CERT_DIR` defaults to the individual
certificates in `nss-certs`. The backend build combines those PEM files into its
own `share/auto-review/ca-certificates.crt` and validates that Python can load CA
certificates from it; this is the default `SSL_CERT_FILE`. Guix normally creates
that combined file in a profile hook, so it is not present in the raw
`nss-certs` package. Explicitly supplied certificate settings remain unchanged.

The packaged frontend defaults to the paired backend's absolute store path.
Source-checkout Lisp continues to default to `pr-review` on PATH. Both allow an
explicit `magit-auto-review-executable` customization.

The host continues to supply enabled Claude/Codex CLIs and their authentication,
GitHub authentication, Git configuration, signing programs/keys/agents, custom
credential helpers, and project tools. SSH agent configuration is inherited.
Terminal draft editing uses `VISUAL`, then `EDITOR`, then host `vi`; the Emacs
frontend's inline editor needs no terminal editor. State remains under
`XDG_STATE_HOME/pr-review` (default `~/.local/state/pr-review`), outside the store.
Package rollback does not roll back saved review data or external host state.

## Validation

`make check-backend` runs the Python and Bats suites, including relocated
execution through a symlink, bundled asset loading, paths with spaces, and
no-write checks. `make check-emacs` runs byte compilation and frontend ERT.

`make check-guix GUIX_CHANNELS=/path/to/channels.scm` builds the real frontend
and backend, then starts a pure Guix shell from a temporary directory with an
isolated HOME. It loads only installed Lisp, tests Forge integration and the
protocol with a shadowing `pr-review` on PATH, and checks the backend's pinned
interpreter/tools, prompt/schema resources, certificate defaults and overrides,
inherited host integration settings, and unchanged installation contents.
These checks make no GitHub or model requests and require no credentials.

The builders also require exactly one match for each interpreter, runtime
environment and frontend/backend pairing patch, validate the generated CA bundle,
and check backend CLI startup.
Changed source layouts therefore fail during the build instead of silently
producing an unconfigured package.

A missing Guix executable or failed build is a failure, never a skipped pass.
Local Python/Emacs checks do not establish that a Guix closure builds. Run the
installed-package check on a suitable Guix host before treating the packaging
as validated; publishing the source alone does not perform that check.
