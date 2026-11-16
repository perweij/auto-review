# GitLab.com review pilot (#25) and execution reports

The pilot validates the shared review engine with GitLab metadata, discussions,
MR pipeline summaries and Git fetching. Ordinary reviews remain read-only.
Explicit execution-report publication is supported separately; finding publication,
Owner, issue fixing, monitor and full Emacs integration remain separate work.

## Explicit execution-report publication

Use `pr-review publish-report SESSION [--generation ID]` to post the selected
generation's frozen report to its MR discussion. `review-publish-report` provides
the same protocol operation. Authenticate as the account that ran the review.
No models run, and an unchanged snapshot returns its existing note URL.

Report notes use a separate capability from finding publication. Their journal
records the destination, actor and body before sending; recovery locates the
marker before checking content. Edited/duplicate matches and incomplete lookups
block sending. Unknown delivery with no visible match remains uncertain rather
than being resent. Reports preserve the recorded SHA after MR advancement or
closure. Markdown neutralizes slash commands, mentions and object references.

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

- Finding publication (building on completed #21) needs explicit GitLab note,
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

## Controlled smoke evidence — 2026-11-14

The user supplied a private GitLab test project and authorized fixture creation.
The [functional smoke suite](live-forge-smoke-tests.md) exercised native MR
metadata, checkout SHA verification, discussion collection, review and completed
result reuse. Both scripted providers and real economical models passed review
and resume. An additional scripted check pushed a second revision and verified
that a positioned discussion became outdated. Application-operation tracing found
no GitLab mutations; fixture setup uses separate native API operations. Unsupported
publication, Owner and issue commands were rejected.

Real-model evidence: `/tmp/auto-review-forge-smoke-93n88ef4/report.md`.
Extended scripted evidence: `/tmp/auto-review-forge-smoke-7t0_zytw/report.md`.
No fork MR or actual pipeline execution was tested. This historical evidence does
not validate report writes or full Emacs integration; report publication has its
own smoke scenario below.

## Report validation

Run `python3 tests/smoke-forges.py --suite smoke --scenario review-report` with
both controlled projects authenticated. This uses scripted providers and tests
native GitHub/GitLab report notes, accepted-write/lost-response recovery,
idempotency, unchanged review results and absence of new model calls while
publishing. Retain the run's report and URLs. Hosted Mermaid rendering requires
browser inspection; API success alone does not establish it.

### Controlled report evidence — 2026-11-16

Scripted run `state/forge-smoke/run-c9zffjx0` passed all seven preflight, review,
report and integrity scenarios on GitHub and GitLab. Each report recovered an
accepted write whose response was hidden, then reused the same receipt through
CLI and API without another note or model call. Native API checks verified exact
frozen content, unchanged MR state and no reference side effects. Markdown preview
confirmed that the test mentions and object references remained inert.

- [GitLab report on MR !14](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/14#note_4243377432)
- [GitHub comparison on PR #49](https://github.com/perweij/temp-eval-auto-review/pull/49#issuecomment-6914227600)

Local Mermaid parsing passed for the generated report and a larger synthetic
report containing both pie charts. Hosted GitLab browser inspection was blocked
by sign-in/security verification; the GitLab link above remains the assisted
visual check. This evidence does not establish hosted diagram rendering.
