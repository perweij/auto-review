# Reviewer and PR-owner toolkits

Status: non-interactive owner mode implemented and validated on
`feat/pr-owner-toolkit`, created from Python-migrated `main` at `f7f0f2e`.
This document records the implementation contract and subsequent milestones.
Interactive owner `F` remains deferred to its separate work package.

Usage: [terminal workflow](../README.md#pr-owner-workflow),
[Emacs keys and recovery](../emacs/README.md#pr-owner-toolkit), and
[protocol-1 owner operations](api.md#owner-toolkit).

## Summary and scope

Introduce explicitly selected **Reviewer** and **Owner** toolkits, with separate
screens, commands, and workflow state. Preserve the current reviewer workflow
and leave room for additional toolkits, including the planned
[Repository review toolkit](proactive-review.md).

Owner mode handles incoming PR review feedback through automated fixes, human
inspection, commit association, and staged replies. A provider may order a group
of feedback items by dependency and combine related concerns into coherent fix
tasks. Execution remains sequential in a dedicated owner worktree.

Providers plan, modify code, test, and review automatically. The user controls
staging code, committing, pushing, publishing replies, and resolving threads.
Use native Magit for the Git operations rather than building another staging or
commit interface.

This work package includes:

- Toolkit selection and an owner inbox independent of reviewer runs.
- Explicit local handoff of selected saved reviewer findings and fixes on
  self-authored GitHub feedback.
- Individual non-interactive `f` fixes and dependency-aware group `f` fixes.
- Separate fix and automatic-review provider/model/effort settings.
- One repair attempt after blocking review findings at each review checkpoint.
- Durable checkpoints, cancellation, recovery, and partial-batch replanning.
- Automatic exact-match commit association, explicit association otherwise, and
  staged commit-linked replies after push verification.
- CLI, version-1 JSON API, and Emacs support through shared application services.

Separate work:

- Interactive owner-mode `F`, terminal integration, and its feasibility test.
- CI repair, merge administration, and requesting another review.
- Automatic commits, pushes, reply publication, or thread resolution.
- Implementing the Repository review toolkit itself.

The [TODO list](todo.md) tracks this package and the subsequent interactive work.

## Preparation and relationship to earlier work

Implementation began after the Python migration and its dogfood fixes merged.
`feat/pr-owner-toolkit` was created from `f7f0f2e`; development uses the existing
checkout. This is distinct from the dedicated owner worktrees created by the
product. The baseline passed 161 Bats tests, 94 Python tests, and 63 ERT tests.

Follow the architectural contracts in the
[Python backend migration plan](python-migration.md): one Python application
core, thin CLI/API adapters, explicit service dependencies, and shared workflow
rules. Owner orchestration belongs in Python. Reuse the migrated Git, GitHub,
provider, process, storage, draft, and publication services; do not introduce new
Bash orchestration or fabricate reviewer manifests to access those services.

The shared discussion collector now exposes a session-independent fetch, and the
shared GitHub publisher/receipt validator supports PR timeline comments through
issue endpoints. Repository discovery and toolkit preferences are additive;
reviewer discovery and integrity rules retain their behavior.

Owner use cases are in `owner.py`, with separate `owner_state.py`, `owner_git.py`,
`owner_providers.py`, `owner_fixes.py`, and `owner_publication.py` services. Both
`owner_cli.py` and the protocol dispatch call these services directly. The Emacs
workflow is in `magit-auto-review-owner.el`. The existing Guix package copies all
these Python/Emacs modules and prompts; no new runtime or terminal dependency is
introduced.

Keep this document current as milestones land, recording validation and remaining
work. No implementation milestone below is complete merely because its plan has
been documented.

## Toolkit selection and owner inbox

### Selection and buffers

Add an explicit toolkit chooser, remember its selection per repository, and
default existing installations to Reviewer. Register available toolkits without
showing placeholders for workflows that have not been implemented. If the
Repository review toolkit already exists when this work lands, register its
existing entry point without redesigning that workflow.

Use separate buffers, keymaps, menus, and workflow modules. Switching toolkits
selects the appropriate buffer without killing the previous one, preserving
folds, point, unsaved editing buffers, drafts, and running operations. Keep
workflow-specific commands out of other toolkits' menus. Existing reviewer keys,
including its conversation-checking `F`, remain unchanged. Owner `F` is unbound
in this package.

Owner mode opens a PR without requiring a saved reviewer run. Existing repository
PR discovery is available in either toolkit. Switching toolkits is explicit;
being the PR author does not silently select a mode or grant GitHub permissions.

The reviewer `o` action sends a selected finding, status group, or sibling headings
to Owner, where the exact selection is retained for `f`. This is a local snapshot
of saved wording, original findings, reviewed commit, and code/discussion context.
It neither changes reviewer staging nor invokes a model. Saved source revisions
are validated; identical imports reuse the same item, and explicitly re-imported
edits retain the previous copy and invalidate dependent assessments. GitHub refresh
preserves these local items without implicitly following later reviewer edits.
Local findings can have fix outcomes and commit associations, but no GitHub reply
or resolution destination is fabricated for them.

### Refresh and layout

Opening uses cached information. `g` reloads saved results; `G` explicitly fetches
GitHub. Show the last successful refresh and preserve cached information after
failure. Fix and publication operations perform their own freshness checks.
Opening, browsing, and local triage never invoke a model.

| Owner section | Contents |
| --- | --- |
| Attention needed | Unresolved threads, new or edited incoming comments/review summaries, and outstanding change-request reviews |
| Your GitHub comments | Self-authored nonempty feedback available for explicit fixes without unread alerts |
| Fixes | Batch plans, task progress, patches, checks, review findings, and commit associations |
| Replies | Unstaged, Staged, and Published outgoing replies |
| Deferred / History | Deferred feedback, handled items, previous attempts, and saved drafts |

Use stable GitHub identities for feedback and preserve local triage across
refreshes. Initial incoming nonempty feedback is unacknowledged until handled or
explicitly acknowledged. New or edited discussion returns dependent items to
attention and invalidates stale assessments. The user's own messages are fixable
without generating self-notifications. They initially appear in Your GitHub
comments; explicit queueing, deferral, or handling remains local triage. This
supports a person acting as both owner and reviewer without changing GitHub
permissions or verdict rules.

Keep these dimensions separate:

- Local handling and unread/changed state.
- Whether the concern has been addressed in code.
- Whether a reply has been published.
- GitHub thread resolution and outdated-code status.
- Reviewers' submitted verdicts.

Outdated unresolved threads remain visible. Empty review verdicts are status
items rather than invented code-fix tasks. Resolving all threads does not itself
clear a Request changes verdict. See the existing
[GitHub review-process overview](github-review-process-overview.md).

Offer fix, reply/clarify, verify-existing-fix, acknowledge, defer/restore, and
explicit resolve/reopen actions where applicable. Deferral changes local triage
only. Ordinary replies are editable local drafts; automatic staging is reserved
for the verified commit-linked reply flow described below.

## Backend boundaries, storage, and interfaces

### Shared services and owner state

Share narrowly scoped services for repository discovery, complete GitHub context,
provider metadata, process supervision, contextual rendering, draft editing, and
publication recovery. Keep reviewer and owner orchestration separate. Never
weaken reviewer integrity checks to accommodate owner modifications.

Store owner sessions under a distinct `owners-v1` subtree of the existing state
root, keyed by checkout identity, repository identity, and PR number. Use
`session.json` with explicit owner kind and format/version fields. Owner sessions
must not be discovered as reviewer generations or appear in reviewer history.

Persist:

- Repository, PR, account, worktree, and push-target identity.
- Versioned GitHub context and local triage.
- Owner settings.
- Batches, plans, task attempts, checkpoints, reports, and process records.
- Commit associations, drafts, publication intents, and receipts.

Use the shared storage service's restrictive permissions, atomic writes, and
recovery journals. Retain history and pin referenced Git objects; do not introduce
automatic pruning.

Owner settings have their own schema: independent fix and review
provider/model/effort triplets plus verification policy. Initialize both triplets
from the existing resolved default processing-provider configuration, without
requiring a reviewer session. Thereafter they remain independent. Reuse
provider-backed model/effort selection and validation; never silently substitute
an unsupported model.

### Locks and process ownership

Use a long-lived owner worktree execution lock for provider operations and
reconciliation, short owner-state locks for atomic updates, and the existing
shared PR lock around GitHub publication and thread mutations. Owner fixes do
not hold the reviewer PR lock throughout inference.

Follow the Python migration's lock order: when needed together, acquire the
owner execution lock, then the shared PR lock, then the owner state/draft lock.
Never acquire them in reverse order. State-only transactions do not subsequently
acquire the PR lock; snapshot and release first, then reacquire in order and
validate the revision. Conflicting operations return `busy`; do not wait
indefinitely while holding another lock.

Do not hold state/publication locks across inference, editor sessions, or human
confirmation prompts. Preparation and final acceptance revalidate revisions.
Cached reads remain available while a fix runs. Process descendants must not
inherit locks that keep the session busy after the owning process exits.

External Magit edits do not obey application locks. Detect them with checkpoint
and Git-state checks, stop for reconciliation, and preserve their content.

### Public interfaces

Extend protocol 1 additively and advertise owner capabilities. Provide owner
operations for discovery/opening, reads, refresh, triage, settings,
fix/resume/cancel, commit association, and publication preparation, execution,
and recovery. Mutations require a current session revision. Long operations use
opaque operation IDs and the existing NDJSON progress/result/error conventions.

Expose equivalent operations under `pr-review owner`. Both CLI and API call the
same application services; neither adapter launches the other to reuse behavior.
Non-interactive fix execution never publishes. Existing reviewer requests and
invocations retain their defaults and behavior.

## Provider execution

### Roles and command construction

| Role | Source changes | Inputs |
| --- | --- | --- |
| Planner | Disallowed | Selected feedback, complete discussion, current code, and accumulated changes |
| Fixer | Allowed | Task plan, per-item expected outcomes, and current checkpoint |
| Reviewer | Disallowed | Exact before/after diff, resulting code, original feedback, and reported checks |

The fix-provider configuration handles planning, implementation, and repair.
The independent review configuration handles result verification. Default to the
existing focused verification policy; retain its per-check and cumulative limits
and its distinction between model instructions and enforced process timeouts.

Implement role-specific command builders through the shared provider adapters:

- **Codex:** structured `exec`, selected model/effort, JSON events, ephemeral
  sessions, and `approval_policy=never`; `workspace-write` for fixing and
  `read-only` for planning/review.
- **Claude:** print mode, selected model/effort, structured streamed output, no
  session persistence, and non-interactive permission handling. Fixing permits
  editing tools; planning/review retain existing editing restrictions. Retain
  denials for Git workflow mutations and GitHub commands.
- Preserve credential filtering, authentication probes, configurable limits,
  cancellation, and trusted-project configuration behavior. Do not use permission
  or sandbox bypass flags.

Validate required CLI capabilities before execution. Add targeted smoke checks
for the new write-capable invocations in the supported environment. Missing
capabilities stop the operation rather than selecting weaker settings. Pin the
resolved settings and actual commands in attempt records so resume is auditable.

### Integrity and trust

Replace the reviewer clean-tree invariant only within owner operations:

- Planner and reviewer must leave the captured source tree and real index
  unchanged, even though the source tree may already contain uncommitted fixes.
- Fixer may alter source files, but must leave HEAD, the real index, owner branch
  identity, and Git administrative configuration unchanged.
- Record permission denials and unexpected changes. Stop before downstream work
  when integrity fails; do not reset the worktree to conceal the failure.
- Apply existing agent-configuration trust checks to owner inputs. Changes to
  loaded agent configuration invalidate that trust before another invocation.

The orchestrator performs its own checkpoint/ref writes outside provider
invocations. Providers do not stage, commit, push, publish, or resolve threads.
Treat feedback as task data, not authority to change these constraints.

These controls retain the project's trusted-repository/container assumptions.
Tool restrictions and post-run detection are guardrails, not a complete isolation
boundary. Do not present command-pattern denials as proof that arbitrary shell
code cannot bypass them.

## Individual non-interactive fixes

Pressing `f` on an actionable item:

1. Refresh PR identity and relevant discussion; capture source and discussion
   fingerprints.
2. Plan a response, allowing already-addressed, clarification-needed, or
   unjustified-change outcomes rather than forcing a code change.
3. Implement a justified fix and perform focused verification.
4. Review the resulting patch against the original concern and inspect for
   regressions.
5. For blocking findings, allow one automatic repair attempt and review again.
6. Present the plan, patch, checks actually performed, limitations, and per-item
   outcome.

Failed or incomplete verification pauses automation and cannot produce a
successful automatic fix outcome. Preserve useful patches and reports for human
inspection. The user can retry, skip, or manually edit the owner worktree and
explicitly request reverification. Interactive provider handoff is deferred.

## Dependency-aware group fixes

### Planning and validation

Pressing `f` on a group or selected sibling items snapshots that selection, then
asks the fix provider to inspect the discussions and relevant code. The planner
may combine overlapping concerns into one coherent task and identify which
tasks depend on others.

Each task records its ID, source-item IDs, prerequisites, rationale, and expected
outcome for each original item. Validate that every selected item belongs to
exactly one task, no unselected feedback was silently added, all prerequisites
exist, and the dependency graph is acyclic.

Invalid planning output stops before source changes. Preserve diagnostics and
offer an explicit planning retry. Planning proceeds automatically without a new
approval checkpoint; its result is visible in the GUI.

### Sequential execution

Execute tasks sequentially in dependency order in one owner worktree. Displayed
order breaks ties between independent tasks; for combined tasks, use the first
source item's displayed position.

Before each task, check whether earlier changes already satisfy its feedback.
Verify those outcomes instead of implementing duplicate fixes. Retain every
original item's identity, outcome, and reply destination even when several items
share a task.

Save per-task snapshots, patches, executed checks, and review reports. Review
each task and permit one repair attempt. After all tasks finish, review the
complete accumulated diff and all intended outcomes, permitting one further
repair attempt at that final checkpoint. Per-task review remains included;
model usage is not reduced by silently skipping it.

Later tasks may modify code touched by earlier tasks. Preserve earlier snapshots
and mark affected earlier verification superseded. The final review assesses all
completed fix outcomes against the final tree.

Pause on execution failure, clarification requiring user input, incomplete
verification, or remaining blocking findings. Skipping an unfinished prerequisite
blocks its dependent tasks; independent tasks may continue after explicit resume.

### Baselines, replanning, and checkpoints

Keep the original batch HEAD immutable as its commit-association baseline.
Replanning unfinished work uses the current accumulated tree, completed outcomes,
and the diff from that original baseline. Never silently restart planning from
the remote PR head and lose the context of completed work.

Create tool-owned Git tree checkpoints through a temporary index. Capture tracked
content and nonignored untracked files; record ignored/generated files separately.
Pin objects under private owner refs. Do not alter the real index, create
user-visible commits, or use the user's stash. Recorded tree objects are internal
recovery artifacts, not automatic fix commits.

Persist transitions before launching a provider and after capturing its result.
Record process identity using the existing PID/start-time mechanism. On a crash
or interruption:

- Preserve the partial working tree and earlier completed checkpoints.
- Mark an orphaned running attempt interrupted.
- Show its diff from the last completed checkpoint.
- Require explicit continuation or manual reconciliation; do not blindly replay
  a partially applied task from its old inputs.

Manual edits invalidate affected reviews. An explicit adopt-and-reverify action
captures them without changing the original batch baseline. Existing reports and
human edits remain available in history.

Changed discussion may trigger renewed planning for unfinished tasks against the
current accumulated changes. Changed local HEAD or an external PR-head update
pauses execution. The user reconciles through Magit; a new batch then records the
new baseline while retaining the earlier batch and its artifacts.

## Owner worktree and Magit handoff

Create a real local owner branch and dedicated worktree from the verified PR
head, with a unique owner-specific name. Do not reuse reviewer snapshots or
collide with an author branch already checked out elsewhere. Existing worktrees
are never overwritten.

Record the actual head repository and branch, including forks. Extend metadata
collection where needed. PR fetch refs supply source objects; they are not push
destinations.

If an existing remote matches the head repository, show and configure the
explicit owner-branch push mapping. If none matches, display the required
destination and let the user configure it through Magit; do not silently add a
remote. Display ambiguity rather than guessing among matching remotes.

New batches require clean source/index state. Existing unpushed owner commits
are allowed when the freshly fetched PR head remains an ancestor of local HEAD;
record both identities. Divergence requires manual reconciliation.

Provide **Open Magit** for inspection, staging, committing, and pushing, showing
the intended PR destination. Human Git operations retain normal hook behavior;
tool-owned Git plumbing follows the existing hooks-disabled convention. Never
automatically reset, stash, rebase, or discard user work.

## Commit associations and replies

### Association rules

Automatically associate a commit only when its parent equals the batch's original
baseline commit, its complete tree equals the final verified batch tree, and all
associated feedback items have successful code-fix outcomes.

Items classified as clarification, no justified change, or already addressed do
not automatically receive a fixed-in claim. Partial commits, split commits,
additional changes, and ambiguous matches require explicit item-to-commit or
commit-range association. Save actual SHAs and provenance. Overlapping filenames
alone do not establish an association or prove a fix.

Refresh associations on request and before generating or publishing replies;
do not depend on observing every Magit command. Amendments and rebases invalidate
obsolete associations and require fresh verification or explicit reassociation.

### Generating and publishing replies

Fetch fresh PR metadata and source objects, then verify associated commits are
ancestors of the PR's reported current head. Use that PR identity rather than
assuming an arbitrary local tracking ref or similarly named branch is current.
Unpushed or unreachable commits block fixed-in publication.

After verification, generate editable commit-linked replies directly into
**Staged**:

- Inline feedback replies to its original thread.
- General comments and review summaries receive a PR timeline comment linking
  their source, since they have no corresponding inline-thread reply endpoint.
- Combined tasks retain every original destination.
- Produce one generated reply per destination per batch, including the relevant
  commit links rather than duplicating posts for combined feedback.

Generalize the shared GitHub request and publication receipt machinery to support
issue-comment endpoints and receipts. Reuse marker-based recovery rather than
building a parallel publisher. Preserve human-edited wording when refreshing or
regenerating proposals.

Publication requires explicit user action and fresh account, destination,
discussion, and commit-reachability checks. Changed wording, associations,
discussion dependencies, or unreachable commits unstage affected unpublished
replies. Never silently rewrite a human-edited draft to accommodate those changes.

Record publication intent before sending. Partial success moves only confirmed
replies to Published. Uncertain responses require read-back recovery before retry;
never blindly resend a possibly successful post. Already-published replies remain
historical receipts even if their commits are later rewritten.

Publishing never automatically resolves threads. Resolve/reopen remains a
separate permission-checked action and does not change review verdicts.

## Delivery milestones

The milestones below define the non-interactive package and its exit criteria.
Milestones 0–5 are complete in this branch. Group planning and per-task review
are included in the implementation; the validation record is below.

| Milestone | Implementation evidence |
| --- | --- |
| 0. Preparation | Python migration baseline inspected; feature branch and baseline checks recorded above |
| 1. Toolkit/inbox | Explicit repository preference, isolated owner sessions, complete discussion/triage, CLI/API, separate Emacs buffers and settings |
| 2. Individual fixes/recovery | Real owner branch/worktree, role-specific providers, source/index/admin checks, pinned trees, cancellation, adoption, partial diff, native Magit |
| 3. Dependency-aware batches | Exact item coverage and per-item expectations, acyclic planning, stable order, combined tasks, task/final review with bounded repairs, accumulated-change replanning |
| 4. Commit-linked replies | Exact/explicit associations, fork targets, fresh reachability, per-destination drafts, durable intents/receipts, partial and uncertain publication recovery |
| 5. Integration/docs | Usage/API/TODO updated; full backend and Emacs checks plus live Claude and Codex planner/fixer/reviewer smoke tests passed |

### Milestone 0: rebase the plan and prepare implementation

- Inspect the earlier feature's completed architecture and reconcile shared
  services, package locations, lock order, and current provider capabilities.
- Create the feature branch from the then-current `main`.
- Establish the supported backend and Emacs baseline checks.

Exit criteria: the branch and baseline exist, this document matches the actual
integration points, and existing behavior has a recorded verification baseline.

### Milestone 1: toolkit shell and owner inbox

- Add toolkit selection, separate buffers, owner settings and state, discovery,
  API/CLI entry points, and capability negotiation.
- Collect and display complete feedback with cached/local and explicit GitHub
  refresh, triage, and independent status dimensions.

Exit criteria: an owner can inspect and triage feedback without a reviewer run
or model invocation; reviewer workflows remain compatible.

### Milestone 2: individual automated fixes and recovery

- Add the dedicated owner worktree, role-specific provider execution, integrity
  and trust checks, checkpoints, and individual fix/review/repair flow.
- Implement cancellation, interrupted-task inspection, manual reconciliation,
  and Open Magit.

Exit criteria: a single concern produces an inspectable, verified patch or an
honest non-success outcome; interruption preserves work and never commits,
pushes, or publishes automatically.

### Milestone 3: dependency-aware batches

- Add validated provider plans, combined tasks, prerequisite ordering, and
  per-item outcomes.
- Implement accumulated-change context, earlier-fix recognition, final review,
  blocked dependents, and partial-batch replanning.

Exit criteria: related items can share a fix, dependencies execute correctly,
and failures/replanning preserve both original identities and completed work.

### Milestone 4: commit-linked replies

- Add exact-match and explicit associations, fork destination handling, and
  fresh remote-head reachability checks.
- Generate staged thread/timeline replies and integrate publication intent,
  receipt validation, and uncertain-response recovery.

Exit criteria: user-created and pushed commits can be linked to the right
feedback, and explicit publication sends each intended reply once without
automatically resolving threads.

### Milestone 5: integration, packaging, and documentation

- Complete backend/Emacs integration checks and provider execution smoke tests.
- Update usage, API documentation, packaging where required, and milestone/TODO
  status. Document manual reconciliation and recovery paths.

Exit criteria: this non-interactive package meets the validation requirements
below, existing interfaces remain supported, and documentation distinguishes
implemented behavior from the later interactive package.

### Later milestone: interactive fixes

This is a separately planned work package after the non-interactive foundation.
It must not add terminal dependencies or acceptance gates to milestones 0-5.

First validate both Claude and Codex in the supported Emacs environment. Evaluate
vterm first, then Eat if needed. Record versions, reproduction steps, and results
for rendering, multiline input, Unicode, scrolling, resizing, streaming output,
and provider dialogs. Verify an Emacs-owned escape binding, interrupt behavior,
process-group cancellation, buffer/Emacs closure, and safe completion before
automatic review starts. If neither integration passes, revise the interactive
design before implementation; do not claim compatibility from mocked tests.

Only after that experiment, implement `F` for individual tasks and sequential
groups using the existing owner worktree, task plans, checkpoints, settings,
outcomes, and review pipeline. Define an explicit finish-and-review handoff and
ensure no interactive writer remains active when automatic review begins.

Exit criteria for that later package: both supported provider CLIs pass the live
terminal acceptance matrix, the user can reliably escape/cancel/return, and
interactive work participates in the same recovery and publication rules.

## Validation and completion

Use mocked GitHub/providers, real temporary Git repositories, Python tests, the
existing shell/Bats coverage where still applicable after migration, and Emacs
ERT. Test observable behavior and compatibility rather than implementation
details. Required scenarios include:

| Area | Required coverage |
| --- | --- |
| Toolkits and state | Reviewer compatibility, isolated menus/keymaps, remembered selection, separate buffers, owner discovery, and sessions without reviewer history |
| Incoming feedback | Pagination, outdated unresolved threads, independent verdicts, own-message context, edited feedback, deferral, and failed refresh |
| Concurrency | Owner writer exclusion, publication coordination with reviewer operations, lock order, revision conflicts, and externally edited worktrees |
| Provider execution | Role permissions, dirty-tree review integrity, unexpected Git changes, changed agent configuration, unsupported capabilities, and incomplete verification |
| Group planning | Complete item coverage, invalid/cyclic plans, deterministic tie-breaking, combined tasks, blocked prerequisites, and no silent scope expansion |
| Group results | Earlier tasks satisfying later feedback, overlapping changes, superseded verification, final combined review, and individual destinations within combined tasks |
| Recovery | Temporary-index isolation, pinned checkpoints, cancellation, crash between transitions, partial task edits, adoption/reverification, replanning, and remote-head changes |
| Git handoff | Exact/partial/split commits, extra edits, amendments, forks, missing/ambiguous remotes, unpushed commits, divergence, and force-push invalidation |
| Publication | Correct thread/timeline destinations, human-edit preservation, stale staging, account changes, partial success, and uncertain-response deduplication |

Run the complete supported backend and Emacs checks after integration. Validate
the new provider write invocations through focused smoke tests in the supported
environment; automated mocks do not establish actual CLI permission behavior or
semantic model accuracy. Live terminal acceptance belongs exclusively to the
later interactive package.

### Implementation validation record

Validation on 2026-10-25 passed:

- `make check-backend`: lint, all 161 Bats acceptance tests, and all 141 Python
  tests (including 47 owner tests).
- `make check-emacs` with Emacs 30.2 and the supported Magit/Transient load file:
  warning-free byte compilation and all 77 ERT tests (including 14 owner tests).
- `git diff --check`, local documentation links, and Python 3.9 grammar parsing
  for the production modules, entry point, and owner smoke script.
- The live provider smoke checks described below, using disposable repositories.

Owner tests use real temporary repositories/worktrees and mocked GitHub/model
responses. They cover combined plans, invalid dependency/coverage/expectation
output, focused verification failures, bounded task/final repairs, interrupted
attempts, adoption/replanning, private trees and real-index isolation, changed
configuration trust, Git administrative changes, fork mappings, exact/extra/split
commits, amendments, force-push invalidation, account changes, stale edits,
thread actions, partial publication, and marker read-back. Existing collection,
pagination, process cancellation, and reviewer regression suites exercise the
shared services. ERT covers isolated keys, display-order group selection, settings,
fold retention, editor recovery, partial diffs, trust inspection, explicit
publication, and native Magit handoff.

Real provider acceptance on 2026-10-25 used
`python3 tests/smoke-owner.py --provider claude --model haiku --effort low` and
`python3 tests/smoke-owner.py --provider codex --model gpt-6-luna --effort low`.
Each performed live authentication, structured read-only planning, a source edit
and focused assertion, and structured read-only review in a disposable linked
worktree. Both passed; HEAD, index, branch, and Git administrative state were
preserved. GitHub was not contacted and nothing was published. These smoke tests
incur model usage; they establish supported invocation behavior on a small
fixture, not general correctness of model judgments. Codex required the supported
environment's access to its CLI runtime state outside the restricted workspace.

The PR #4 follow-up on 2026-10-25 added explicit Reviewer `o` handoff and fixing
self-authored GitHub feedback. Validation passed all 161 Bats tests, 151 Python
tests (57 owner tests), and 83 ERT tests, plus lint and warning-free byte
compilation. Added coverage includes saved-wording/context preservation, exact
group selection, stale revisions and busy locks, idempotent imports and changed
source versions, local items surviving refresh, mixed local/GitHub fix batches,
commit associations without invented reply destinations, and replies to the
original self-authored thread. These checks use temporary repositories and
mocked providers/GitHub; this follow-up did not run live inference or publish
anything. The provider invocation contract remains unchanged.

The implementation adds no Guix dependency or package-layout change. The existing
migration follow-ups for a real Guix build and execution on Python 3.9 remain in
the [TODO list](todo.md); this workspace has Python 3.12 and verified Python 3.9
syntax. Interactive terminal acceptance remains exclusively a later milestone.

Completion means milestones 0-5 satisfy their exit criteria, accepted fixes and
replies remain inspectable and recoverable, no existing reviewer behavior has
regressed, and all external workflow actions remain explicit user decisions.
