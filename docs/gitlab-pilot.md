# GitLab.com read-only pilot (#25)

The pilot validates the shared review engine with GitLab metadata, discussions,
MR pipeline summaries and Git fetching. It intentionally leaves remote writes,
Owner, issue fixing, monitor and full Emacs integration for separate work.

## Live procedure

Use an existing controlled GitLab.com project with a small open MR, authenticated
`glab`, a local clone of its target repository, and authenticated model CLIs.
Run `pr-review --forge gitlab URL`, inspect the logged checkout and normalized
context/diagnostics, then resume the session. Confirm the verified source SHA,
local reports, complete discussion matching, explicit CI coverage and absence of
remote publication. Repeat with a fork MR and a populated discussion if available.
Do not create projects, MRs, comments or pipelines as part of this procedure.

## Observed live availability — 2026-11-11

The provided credentials successfully accessed GitLab.com project discovery using
explicit GET requests. Owned-project and membership-project queries restricted to
Maintainer-or-higher access both returned empty lists. No controlled project was
identified, so live MR, clone, model-pipeline, fork and discussion validation was
not performed. Mocked tests must not be represented as live results. Guix is not
installed in this workspace, so an installed Guix build is also unavailable.

## Abstraction gaps and follow-ups

- Publication (#21 and a future GitLab write issue) needs explicit GitLab note,
  discussion and approval semantics; native diff positions are retained for this.
- Owner (#22) must distinguish reply permissions, resolvability and outdated
  positions; this pilot advertises no mutation permissions.
- Issue work (#23–#24) needs GitLab eligibility and write contracts separately.
- MR listing, focused follow-up and frontend capability-aware menus need separate
  follow-ups. The pilot supports the explicit CLI/API review path only.
- CI coverage is MR pipeline summaries for the reviewed SHA. Merged-result,
  downstream and external checks need a richer coverage contract before inclusion.

References: https://docs.gitlab.com/api/merge_requests/ and
https://docs.gitlab.com/api/discussions/ .

Offset pagination detects duplicate records but cannot establish an atomic snapshot
while discussions are concurrently deleted or edited. Collection is complete with
respect to the pages returned by the API; it is not a consistency guarantee across
concurrent remote mutations.
