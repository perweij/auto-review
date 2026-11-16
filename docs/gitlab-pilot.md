# GitLab.com review, COMMENT publication and execution reports

The pilot validates the shared review engine with GitLab metadata, discussions,
MR pipeline summaries and Git fetching. `--no-publish` reviews remain read-only.
COMMENT finding publication and explicit execution reports are supported;
Owner, issue fixing, monitor and full Emacs integration remain separate work.

## Finding publication

Use the existing interactive CLI/API preview and confirmation flow, or explicitly
request `--auto-publish --json`. Overall assessments become timeline notes; inline
lines/ranges become positioned discussions. Additions, deletions, unchanged
context and renamed/new/deleted files use the validated Git snapshot and a
collected native diff version. No inline placement is silently downgraded.

The journal freezes each native operation and binds project/MR/account identities,
body, marker and position. Partial publication retains confirmed receipts and
resumes only an unchanged remainder. Identity lookup is separate from content
validation; edited or conflicting candidates block resubmission. Unknown delivery
with no visible receipt remains uncertain. Definite API rejection remains retryable.
Previews retain Markdown and code while neutralizing executable quick actions.

Only COMMENT is supported. Approvals, request-changes, replies, resolve/reopen and
code suggestions are unavailable. Suggestion generation is rejected before model
execution. GitLab workflow recovery instructions use the supported terminal menu.

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
Run `pr-review --forge gitlab URL --no-publish`, inspect the logged checkout and normalized
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

- Finding publication builds on completed #21; approval/verdict extensions,
  replies and code suggestions require separate follow-ups.
- GitLab Owner expansion (building on completed #22) needs reply permissions,
  resolvability and outdated positions; this increment advertises no thread mutation permissions.
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

## Finding validation

`python3 tests/smoke-forges.py --suite smoke --scenario gitlab-publication`
uses scripted providers with native GitLab effects. It checks an inline-first
batch, accepted-response-loss recovery, then an overall-first mixed batch with
removed/context/range anchors, exact bodies, no duplicates, unchanged MR state
and no models during publication. It also exercises automatic CLI JSON output
and completed-result reuse. `make smoke-fast` includes these cases alongside
GitHub publication, Owner, issue and report regressions. No paid model run is needed.

### Controlled finding evidence — 2026-11-16

Full scripted run `state/forge-smoke/run-pu9ap25o` passed all 19 scenarios across
GitHub and GitLab. The GitLab mixed batch lost a response after its overview and
first inline comment were accepted. Receipt recovery and a fresh confirmation
sent only the unchanged remainder, leaving exactly five findings with the
expected native positions and bodies. Automatic CLI publication and its repeat
completed without duplicate notes. Supplied checkouts/default branches were unchanged.

After correcting an HTML/fence rendering edge case, focused native run
`state/forge-smoke/run-vt7k_qc7` passed all six scenarios again. Its overview tests
literal `<pre>` inside a fenced example and literal fences inside a raw HTML
block; subsequent slash commands remain inert and the MR remains open.

- [Overview and rendering fixture on MR !18](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/18#note_4245479686)
- [Recovered multiline finding on MR !18](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/18#note_4245487685)

Backend validation passed 203 CLI tests and the full 1,355-test Python suite.
Following the final renderer correction, all 142 affected Python tests and lint
passed. These runs used scripted providers and consumed no model quota.

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
