# Proactive repository review and issue publication

Status: planned; this document specifies future work, not implemented behavior.

## Summary and architectural direction

Add an on-demand workflow that reviews the repository's default branch, optionally
guided by a natural-language focus. Users inspect findings locally and explicitly
publish worthwhile findings as individual GitHub issues.

Treat this feature as the future **Repository review toolkit**, alongside two
other planned major modes:

| Major mode | Purpose |
| --- | --- |
| PR reviewer's toolkit | Review another contributor's proposed changes |
| PR owner's toolkit | Support the author of a PR; planned separately |
| Repository review toolkit | Proactively investigate existing code and report actionable findings |

The future mode system will show only the selected mode's GUI operations.
**Implementing that mode system, reorganizing the existing reviewer interface,
and building the owner's toolkit are outside this specification.**

Design this feature with its own entry point, setup, results view, and operation
menus so it can become the third mode without another GUI redesign. Share backend
infrastructure and useful UI components, but keep workflow-specific orchestration
and controls separate. Do not add audit-specific branches throughout the existing
PR reviewer menus.

The first version covers defects and concrete risks, with CLI, JSON API, and Emacs
support. Fix generation, branch/PR creation, scheduling, and comments on existing
issues/PRs are excluded.

An opt-in extension for actionable design, maintainability, and performance
proposals is separate future work, tracked in [the TODO list](todo.md). Such
proposals should be distinguished from evidenced defects and use the same triage
and publication workflow.

## Workflow and dedicated interface

### Start and focus

Add `pr-review audit`, supporting existing repository, reviewer, model,
verification-policy, resume, and no-publication options.

Resolve the repository's actual default branch rather than assuming `main`.
Review its remote snapshot regardless of the caller's checked-out branch.

Add mutually exclusive `--focus TEXT` and `--focus-preset NAME` options. Persist
the resolved focus text. When neither is supplied, interactive setup offers
editable presets; noninteractive execution uses General review.

| Preset | Emphasis |
| --- | --- |
| General review | Concrete defects and failure scenarios |
| Correctness | Broken invariants, edge cases, incorrect results |
| Error handling and recovery | Partial failure, retries, cleanup, interruption |
| Security boundaries | Authorization, input handling, unintended exposure |
| Concurrency and state | Races, ordering, persistence, inconsistent state |
| Tests and coverage gaps | Untested behavior tied to a concrete failure risk |
| Custom focus | User-supplied natural-language instructions |

Focus guides investigation without preventing examination of surrounding code.
Reports must describe actual coverage and uncertainty.

### Analyze and triage

- Use the existing single-provider or independent-review-plus-synthesis pipeline
  with audit-specific instructions.
- Report severity, validity, evidence, source locations, verification performed,
  and coverage limitations. Exclude generic cleanup advice and speculative test
  requests.
- Consolidate reports of the same underlying defect. Each distinct finding
  produces one editable issue draft.
- Start drafts Unstaged. Support title/body editing, staging, suppression,
  restoration, history, and commit-pinned source inspection.
- Show relevant existing issues/PRs and matching rationale. Clear duplicates
  start Suppressed; creating a new issue requires an explicit individual
  override. Bulk staging cannot override suppression.
- Unverified findings require successful rechecking before publication. Rejected
  findings are not publishable.

### Publish or exit

Preview one issue per staged finding, including its exact title/body, repository,
and evidence commit. Bodies describe the problem, triggering conditions,
expected/actual behavior, impact, verification gaps, and source links.

Require explicit confirmation. Do not automatically assign labels, people,
milestones, or projects.

Exiting without publication creates no GitHub objects, branches, or commits and
leaves the caller's checkout untouched. Local artifacts, private snapshot refs,
and isolated worktrees remain available for resume.

### Emacs interface

Add `M-x magit-auto-review-audit` as a standalone entry point suitable for a future
mode selector. Until that selector exists, the command directly opens this
workflow.

Provide dedicated audit setup, results, and action menus. Reuse UI components
where practical, but display only repository-review operations:

- Focus and model configuration.
- Finding inspection, source navigation, title/body editing, and staging.
- Existing-item links and matching rationale.
- Rechecking selected findings.
- Issue publication, receipts, history, and cancellation.

Show source excerpts instead of PR diff hunks. Do not expose PR verdicts,
inline-review placement, or thread-reply controls. No intricate integration into
existing reviewer menus is required. Opening saved results remains local and does
not invoke models.

## Backend and API

### Sessions and execution

- Introduce session kind `pr` or `audit`; missing kind in existing manifests means
  `pr`. This identifies the current workflow, without defining the future
  application-wide mode architecture.
- Audit identity includes repository, session ID, default-branch name, reviewed
  commit, resolved focus, and review settings. Do not fabricate a PR number,
  merge-base, or empty diff.
- New audit invocations create sessions; `--resume` explicitly selects one. The
  audit interface discovers saved audit sessions.
- Fetch private refs and create isolated detached worktrees. Reuse
  authentication, environment filtering, integrity checks, cancellation,
  checkpoints, and atomic generation promotion.
- Use ordinary prompt-driven provider execution instead of the PR-specific
  Codex review target and Claude `/code-review` invocation. Preserve provider
  independence and verification limits.
- Add an audit schema containing findings and coverage, without PR verdicts or
  approval-blocking counts.
- Include workflow kind, snapshot, focus, prompts, schemas, and model settings
  in cache identities. Changed analysis inputs create replacement generations;
  previous drafts and receipts remain recoverable.
- Follow the existing trusted-repository model for default-branch agent
  configuration. Do not invent a PR configuration diff.

Keep audit orchestration, prompts, matching, and issue-publication logic in
dedicated modules. Share lifecycle, model execution, Git snapshot,
draft-versioning, and receipt infrastructure through explicit interfaces.

### Existing issue and PR matching

- Match after establishing findings, using the configured processing provider.
- Generate up to three search-term queries per finding. The backend constrains
  searches to the selected repository and includes open and closed issues/PRs.
- Retrieve up to 100 results per query, deduplicate candidates, and inspect up to
  ten relevant candidates per finding.
- Retrieve complete conversations for inspected candidates, including PR review
  discussion where relevant.
- Save queries, inspected candidates, timestamps, truncation, and failures. Say
  "no match found in searched context," not that no duplicate exists.
- Compare underlying defects and failure scenarios. Evaluate whether closed
  issues or merged PRs represent a resolution, regression, or different problem.
- Disclose search bounds. Failed searches and incomplete candidate conversations
  require explicit individual acknowledgment before staging.
- Defer posting follow-up comments and grouping distinct findings into one issue.

### JSON API

Extend protocol 1 additively, advertising audit, issue-publication, focus-preset,
and recheck capabilities.

- Extend `run`, `settings`, and `discover` with `kind: "audit"`. Existing requests
  retain PR behavior; discovery also supports explicitly requesting both kinds.
- Expose preset IDs and editable text through the backend.
- Return audit metadata, source context, related-item matches, search coverage,
  drafts, and recheck status through `read`.
- Add audit placement `{kind: "issue"}` and title editing. Title/body changes
  unstage drafts and invalidate confirmation.
- Add explicit `recheck`, accepting finding IDs and current generation/revision,
  with progress and cancellation.
- Reuse `prepare`, `publish`, and `resolve` with issue actions and receipts.
- Reject PR verdicts, inline/reply placements, and diff requests for audit
  sessions.

Preserve existing PR API behavior and legacy state readability. Ordinary PR
findings do not gain issue publication in this feature.

## Freshness and reliable publication

### Default-branch advancement

Before confirmation and between writes, check repository identity,
default-branch identity/head, and relevant existing-item context.

If a selected finding has not been validated at the current head, pause
publication and offer **Recheck selected findings**. Preparation must not
silently invoke a model.

Rechecking:

- Pins the latest snapshot and uses the processing provider with focused
  verification.
- Reassesses selected findings and their existing-item matches.
- Records whether each finding remains substantiated, is no longer present, or
  is uncertain.
- Preserves original evidence, draft IDs, human wording, and receipts.
- Presents updated evidence and suggested wording separately, without
  overwriting edits.
- Returns affected drafts to Unstaged for inspection. Findings no longer present
  or still uncertain cannot be published.

Interrupted rechecks preserve previous completed results. Further branch
advancement requires another recheck before affected issues are sent.

### Issue publication

Use GitHub's create-issue endpoint. Document Issues write permission; disabled
issues or denied access must not discard local work. See the
[GitHub issue API](https://docs.github.com/en/rest/issues/issues#create-an-issue).

- Bind confirmation to exact titles/bodies, revision, repository/account,
  validated snapshots, and checked matching context.
- Persist action intent and a unique hidden marker before each request.
- Submit sequentially and retain verified issue numbers/URLs.
- Reconcile uncertain outcomes using repository issue listings and exact
  markers, without relying on search indexing or blindly repeating POSTs.
- Preserve partial success; unsent drafts remain available for renewed
  confirmation.
- Serialize audit publication per repository and include local issue receipts in
  duplicate checks across sessions.
- Use one shared draft/publication ledger for CLI and Emacs.
- `--no-publish` and noninteractive execution never write to GitHub.

## Verification and acceptance

Extend mocked GitHub/model tests with real local Git repositories and worktrees.
Cover:

- Default branches with names other than `main`, unrelated checked-out branches,
  and dirty caller checkouts.
- Focus presets, custom text, noninteractive defaults, resume, and cache
  invalidation.
- Single/dual-provider audits without PR-specific commands or fabricated diff
  metadata.
- Evidence locations, severity, duplicate consolidation, and nonpublishable
  uncertain/rejected findings.
- Open/closed issues, active/merged PRs, regressions, search bounds, and incomplete
  context.
- Title/body editing, staging, suppression overrides, history, stale revisions,
  and CLI/Emacs parity.
- Dedicated audit menus containing only audit operations; existing reviewer
  menus and behavior remain intact.
- Branch advancement before confirmation and between writes, including
  interrupted rechecks and preserved edits.
- No-selection exits, permission failures, disabled issues, partial success,
  lost responses, marker recovery, and duplicate prevention.
- Existing PR workflows and legacy session compatibility.

Run `make check-all` when implementing this feature. Optional real-provider
evaluation publishes nothing.

Acceptance: users can enter a dedicated repository-review workflow, investigate
a focused default-branch snapshot, inspect and edit findings, publish selected
findings as separate issues, or exit without remote changes or changes to their
working checkout. The workflow is ready to become the third major mode when the
separately planned mode system is introduced.
