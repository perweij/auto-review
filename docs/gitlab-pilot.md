# GitLab.com Reviewer and Owner workflows

The pilot validates the shared review engine with GitLab metadata, discussions,
MR pipeline summaries and Git fetching. `--no-publish` reviews remain read-only.
COMMENT finding publication and explicit execution reports are supported;
Emacs Reviewer supports the same workflow. Owner supports feedback, native replies,
resolve/reopen and same-project fixing across CLI, API and Emacs. Issue fixing and
monitor remain separate work.

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

Reviewer supports COMMENT only. Approvals, request-changes, Reviewer replies,
Reviewer thread mutations and code suggestions are unavailable. Suggestion generation is rejected before model
execution. Publication recovery is available through the terminal or Emacs `P r`.

## Emacs entry and confirmation

The existing Magit/Forge command opens a selected GitLab MR automatically. With
no selected MR, choose a saved review or enter an MR number/URL; ambiguous remotes
require project selection. Identity travels through setup, resume, history and
child buffers. GitLab defaults to Reviewer; `M` selects and remembers Owner per
project. Saved Owner sessions are offered alongside saved reviews. Unsupported
actions fail locally.

The confirmation preview projects frozen unsent native operations after retry
reuse. It shows the exact outgoing prose and positions, omitting only the exact
appended tracking suffix. Original editor/dashboard wording and saved model
choices remain intact. See the [Emacs reference](../emacs/README.md#gitlab-reviewer).

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
- GitLab Owner now supplies reply permissions, resolvability, outdated positions
  and native conversation writes separately from Reviewer. Fork fixing remains deferred.
- Issue work (#23–#24) needs GitLab eligibility and write contracts separately.
- Remote MR listing and focused follow-up remain separate follow-ups. Emacs
  uses local target resolution, saved reviews and capability-driven menus.
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

## Emacs automated validation — 2026-11-16

`make -j3 check-all` passed: 203 shell tests, 1,370 Python tests, byte
compilation, 196 ERT tests and the batch workflow described below.

The frontend checks passed 196 ERT cases, including GitLab entry paths, project
selection, setup/presets, saved settings, capability-driven menus and local
command guards, history/child-buffer identity, and unchanged editor wording.
The same batch scenario also passed with real Git/backend/Emacs processes and
local forge/model doubles. It exercised declined and accepted previews,
publication, reopen/resume, checkout and execution reports. The captured preview
was compared with frozen payloads and the transport double's notes, including
positions and the exact tracking-suffix omission. No model stages were repeated
by publication or resume.

Initial unauthenticated smoke attempts stopped before fixture creation; their
reports remain in `state/forge-smoke/run-c78ylxjb` and `run-1wuc_col`.
After loading the supplied credentials and selecting the existing GitHub
credential with access to the fixture repository, live validation completed:

- `run-x5crfrk6`: all four GitLab Emacs/preflight/integrity checks passed on
  [MR !23](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/23).
  `gitlab-emacs-evidence.json` captures the declined and accepted confirmations,
  visible preview, frozen payloads, unchanged editor wording, and usage snapshots.
  All five native finding notes matched the frozen bodies and positions, with
  no duplicates or change to MR state. Publication, resume and report actions
  left all four scripted review-stage call counts unchanged.
- [Published execution report](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/23#note_4247035846)
  was also verified after reopen/resume and checkout/report commands.
- `run-32knzktz`: the complete smoke-fast suite finished with 19 passed, zero
  failed or blocked checks after resuming the interrupted GitLab cases. This
  includes native accepted-write/lost-response recovery and mixed-batch retries.

GitLab initially returned HTTP 429 while the suites ran concurrently, reporting
`burst_traffic_per_user_free_plan` with a limit of 100. Validation then paced the
native `glab` transport to one API start per 0.95 seconds through a temporary
wrapper. A later read timeout was retried before any write. Prior failures and
confirmed receipts remain in the retained reports; successful retries did not
repeat confirmed writes. Credential helpers were process-local. No paid models
were used, and fixtures remain available for inspection.

Interactive GUI inspection remains pending and is separate from the automated
batch evidence above.

## Owner workflow

Use `pr-review owner MR_URL open --forge gitlab`, then `refresh` explicitly, or
select Owner with `M` in Emacs. Opening and reading retained sessions use local
state only. Owner collects authoritative GraphQL permissions separately from
Reviewer discussion data, so Owner support does not invalidate Reviewer caches.

Compose, edit and stage replies, then inspect `prepare` / Emacs `P` before
publication. The preview shows the frozen outgoing body, native destination and
whether replying converts a standalone comment into a thread. Slash quick actions
are protected while the original editor wording remains intact. Native root-note
identity preserves local feedback, drafts and verification through conversion.
Resolve/reopen uses explicit desired state and fresh permission checks, followed
by native read-back. Models never resolve discussions.

Fixing, verification, commit preparation/association and Reviewer handoff require
an open same-project MR with a known source branch. Fork MRs retain feedback and
conversation actions. The existing worktree, verification, repair and optional
automatic-commit engine is reused. Push and publication remain explicit. Fixed-in
replies use native GitLab commit links; execution reports keep a separate journal.

Owner reply actions retain `delivery: not_sent | unknown | confirmed`. Definite
non-delivery is retryable. A lost response requires receipt recovery; an empty
lookup alone never proves non-delivery. Unresolved delivery blocks further reply
publication in the session. Edited, conflicting, duplicate or missing confirmed
receipts require inspection and do not authorize resending.

Validation uses native `gitlab-owner` and `gitlab-owner-emacs` scenarios with
scripted providers. Interactive GUI inspection is recorded separately. See
[live smoke instructions](live-forge-smoke-tests.md) for commands and evidence.

## Owner validation — 2026-11-18

- `run-j554hzx5`: all four CLI/API Owner, preflight and integrity checks passed on
  [MR !29](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/29).
  Evidence is retained in `state/forge-smoke/run-j554hzx5/`, including native
  payload/receipt checks, lost-response recovery, fixing and verification,
  explicit push, commit association, resolution/reopening and the
  [execution report](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/29#note_4264190455).
- `run-8x3923ks`: all four native batch Emacs, preflight and integrity checks
  passed on [MR !33](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/33).
  `gitlab-owner-emacs-evidence.json` records confirmation previews, the final
  Owner view and rendered buffer. Actual editing/staging, decline/accept,
  fixing, explicit push, fixed-in publication, thread-state read-back, reports
  and remembered Owner reopening were exercised.

The 21 targeted backend tests cover additional failure and permission cases,
including forks, incomplete pagination/joins, account changes, edited/duplicate
receipts, definite rejection and unknown delivery. ERT covers capability-driven
menus, direct-command guards, identity, completion proofs and frozen previews.

Native testing established that standalone conversion changes note GraphQL IDs
from `Note` to `DiscussionNote`, while retaining the native note/discussion IDs.
GitLab also updates volatile note metadata on thread operations; semantic Owner
fingerprints exclude those changes. The fixture waits for REST metadata and the
MR fetch ref to agree after push; the application retains its stale-head guard.

Earlier failed attempts remain in `run-ghqzpej5` (unrecognized converted note
class), `run-j4658ynd` (post-push ref convergence), and `run-b4l24w7_` (smoke-script
syntax error before workflow execution). They are not counted as passing evidence.
All model providers in these scenarios were scripted doubles. Native API/Git and
batch Emacs evidence do not constitute interactive GUI inspection; that remains
separately pending.

The final `make smoke-fast` run, `run-fk3gavcq`, passed **20/20** scenarios,
including both forges' reviews and reports, finding publication, delivery
recovery, Owner workflows, issue workflows, stale/foreign commit safeguards and
original-checkout/default-branch integrity. Its GitLab Owner fixture is
[MR !37](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/37), with
[the verified report](https://gitlab.com/perweij/temp-eval-auto-review/-/merge_requests/37#note_4264409642).
`gitlab-owner-evidence.json` retains the final Owner view, exact previews and
native note IDs. The earlier full run `run-4_efycla` retained 19 passing scenarios
and a GitLab GET connection timeout; it is not counted as a complete passing run.

Final local validation passed `make -j3 check-all`: **203 shell tests, 1,391 Python
tests and 203 ERT cases**, plus the real-process GitLab Reviewer Emacs workflow
with local transport/model doubles. The latest targeted GitLab Owner run passed
all **21** tests, including retained-session repository aliases and first-handoff
fixing availability. All five Emacs runner-isolation tests also passed after
integrating smoke-script syntax checking into the existing compilation phase.
