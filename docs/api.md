# Auto-review frontend protocol, version 1

`pr-review api` accepts one UTF-8 JSON request line on stdin and emits NDJSON on
stdout. All events have `protocol: 1` and a `type`: `started`, `progress`,
`result`, or `error`. There is exactly one final result/error. Errors exit
nonzero and contain `{code,message,...}`. Request bodies are data, never shell
fragments. Use argv invocation and a pipe, not interpolation into a command.

```sh
printf '%s\n' '{"protocol":1,"command":"capabilities"}' | bin/pr-review api
```

The backend owns state layout and migrations. Clients must use this API, rather
than opening manifests, Markdown artifacts, or publication ledgers. `session`
is an opaque selector returned by discovery; `generation` and `revision` are
opaque identities, not paths clients should interpret.

## Local read operations

| Command | Additional request fields | Result |
| --- | --- | --- |
| `capabilities` | none | Protocol version, features, command names |
| `discover` | `cwd`, optional `pr`, `repository` | Matching saved sessions in this Git common directory |
| `pull-requests` | `cwd`, optional `repository` | Repository choices, cached PR list, last-check time, and matching saved sessions |
| `read` | `session`, optional `generation` | Saved review, comments, contextual rows/threads, staging, last check, operations |
| `history` | `session` | Generations, commits, times, status, saved edits/document counts |
| `diff` | `session`, `generation`, optional `path` | Reviewed changed files, hunks, and patches |
| `file` | `session`, `generation`, `path`, optional `side`, `commit` | Immutable Git blob text and revision |
| `preview-comment` | `session`, `generation`, `revision`, `placement`, optional `title`, `body` | Unsaved comment rendered in context; no draft is persisted |

Omitting `generation` in `read` selects the completed current generation. Pin
the returned identity for subsequent reads; do not silently follow a pointer.
The response separately identifies `current_generation`, `pending_generation`,
and verified running operations. Explicit `g` in the main UI selects current;
history buffers keep reading their pinned generation until the user chooses
`N` to open current. Background completion never replaces the displayed text,
and an open inline editor prevents structural refresh.

Repository discovery is separate from saved review discovery. `pull-requests`
returns `{repositories,repository,checked_at,pull_requests}`; each PR has `pr`,
`repository`, `url`, `title`, `state`, and `sessions`. Cached GitHub rows also have
`draft` and `head_branch`. An unrefreshed saved PR has state `UNKNOWN`. Multiple
repository remotes require an explicit `repository` selection before refresh;
PR numbers and saved sessions are scoped to that repository.

With `refresh: true`, `pull-requests` explicitly contacts GitHub without model
calls. It lists every page of open PRs and fetches the current state of saved PRs
missing from that list. Only a complete successful refresh replaces the cache;
errors preserve the prior list and all review drafts. This uses GitHub's
[pull request endpoints](https://docs.github.com/en/rest/pulls/pulls) and
[`gh api` pagination](https://cli.github.com/manual/gh_api). Opening the repository
list remains local; `g` in that list refreshes GitHub. In an individual review,
`g` retains its existing local-results behavior.

Rows have `old`, `new`, `kind`, `text`, and `highlight`. Comments carry their
actual primary context and full saved thread. `thread_contexts` exposes choices
for manual retargeting, including threads not matched to any finding.

Each comment view includes `severity` (Critical/High/Medium/Low or null),
`severity_incomplete`, and `finding_assessments`. Each assessment has `id`,
`severity`, `severity_source` (`structured`, `report`, or null), and `blocking`
(boolean or null). Grouped comments use the highest known severity and flag
missing ratings. Human comments without finding IDs have null severity.
Structured synthesis ratings take precedence; legacy fallback recognizes explicit
ID/level headings, Severity labels under finding headings, and ID/Severity tables.
Missing or conflicting legacy ratings remain null. These view fields are derived
from the saved synthesis and are outside the draft revision: no migration rewrites
drafts, bodies, placements, staging, history, receipts, or publication confirmation.

The `revision` covers saved draft state. Normal legacy reads are virtual imports:
they do not rewrite files, call models, check credentials, or contact GitHub.
Legacy auto-selected CLI proposals become Unstaged; missing structured anchors
become unplaced. Human text is preserved. A new generation gets fresh drafts,
with the preceding generation retained for explicit recovery.

## Review setup and metadata

`settings` accepts `session`, or `cwd` plus optional `pr` and `repository`, and
returns `{settings,session}`. It reads settings from a pending generation first,
otherwise the current generation. With no saved review it uses the optional
`defaults` object followed by backend defaults. Explicit top-level setting fields
override these values. The operation is local and does not modify saved state.
Legacy settings retain their recorded models/effort/testing policy; absent fields
mean both reviewers, Claude processing, high effort, and focused testing.

`models` accepts `provider: "claude"` or `"codex"` and returns:

```json
{"provider":"codex","models":[{"value":"gpt-6-luna","label":"GPT-6-Luna","efforts":["low","medium","high","xhigh","max"]}]}
```

This example is illustrative: the installed CLI supplies the actual choices.
Discovery uses Codex app-server `model/list` or Claude initialization metadata,
with a neutral temporary directory, no inference prompt, and a 30-second deadline.
Only model metadata is returned; account information is discarded. Hidden,
disabled, or effort-capability-free entries are excluded. The frontend refreshes
before enabling Run and permits only listed values. Model metadata does not
replace live authentication checks. Discovery failures have `discovery_failed`
or `discovery_timeout` codes. Send TERM to its API process to cancel; the backend
terminates the provider process group and reaps its child. Discovery has its own
process lifecycle and does not occupy the review-operation slot.

## Owner toolkit

Protocol 1 advertises additive `toolkits` and `owner` capabilities. Reviewer
commands retain their existing contracts. Owner sessions use separate storage
and revisions, do not have reviewer generations, and are never returned by
reviewer `discover`.

`toolkit` accepts `cwd`, optional `repository`, and optional
`toolkit: "reviewer"|"owner"`. Omit the selection to read the remembered choice
(Reviewer by default); include it to save a repository preference. The shared
`pull-requests` result includes `toolkit`, and its saved session descriptors
belong to that selected toolkit. Owner selection never depends on PR authorship.

| Command | Request fields | Result/behavior |
| --- | --- | --- |
| `owner-open` | `cwd`, `pr`, optional `repository`, `defaults` | Create or read a local owner session; no network/model |
| `owner-discover` | `cwd`, optional `repository`, `pr` | `{sessions}` for the Git common directory |
| `owner-read` | `session` | Saved owner view; available while a fix runs |
| `owner-refresh` | `session`, `revision` | Fetch complete GitHub discussions and metadata; return owner view |
| `owner-mutate` | `session`, `revision`, `operation`, operation-specific fields below | Local atomic change; return owner view |
| `owner-fix` | `session`, `revision`, `items` or `batch`, optional controls below | Plan/fix/review or resume; return owner view |
| `owner-diff` | `session`, `batch`, optional `task` | `{diff,before,after}` for a retained task/batch |
| `owner-associate` | `session`, `revision`, optional `batch`, `items`, `commits` | Associate verified fixes and generate reachable fixed-in replies |
| `owner-prepare` | `session`, `revision` | Fresh `{owner,token,replies}` publication preview |
| `owner-publish` | `session`, `revision`, `token` | Send the explicitly confirmed preview; return owner view |
| `owner-recover` | `session`, `revision` | Read back uncertain submissions; return owner view |
| `owner-thread-state` | `session`, `revision`, `item`, boolean `resolved` | Explicit permission-checked resolve/reopen; return owner view |

All owner mutations require the last displayed revision. Store returned revisions
and use `owner-read` after an error: a long operation can have saved partial work
before it fails. Opening creates a session idempotently without a revision.
`owner-diff` with `current: true` additionally requires `revision`, takes the
execution lock, and pins a current tree for inspection against the last saved
batch checkpoint. It does not adopt that tree or change the real index.

Owner views contain `session`, `revision`, repository/PR identity, `settings`,
`checked_at`, `viewer`, `feedback`, `batches`, `replies`, `actions`, and `worktree`.
After refresh, `metadata` and `push_target` identify the reported PR head and its
actual repository/branch. `push_target.remotes`, when setup has inspected them,
is a list: zero or several matches require user configuration. Artifact paths in
attempts are for opening logs, not for clients to mutate workflow state.

Feedback has stable `key` values (`thread:…`, `comment:…`, `review:…`), full
`comments`, `fingerprint`, `notification`, `triage`, `unread`, `actionable`, and
`attention`. Thread entries retain resolution/outdated/permission flags and the
original code hunk. Review entries retain submitted `state` and
`change_requested`. An empty verdict can require attention without being
actionable. Local triage does not resolve GitHub threads or clear review verdicts.

Owner mutation operations:

| Operation | Fields |
| --- | --- |
| `settings` | `settings: {fix: {provider,model,effort}, review: {provider,model,effort}, verification: "focused"}` |
| `acknowledge`, `defer`, `restore`, `handled` | Distinct `items` array |
| `reply` | Original feedback `item`, nonempty `body` |
| `edit` | `reply` or `replies`, nonempty `body` |
| `stage`, `unstage`, `acknowledge-reply` | `reply` or distinct `replies` array |

Only new owner sessions use `defaults`, whose fields match reviewer setup
defaults; the resolved processing triplet initializes both owner roles. Settings
are subsequently independent and validated against fresh CLI metadata before
execution. Metadata selection uses the existing `models` command.

For a new `owner-fix`, `items` is a nonempty ordered snapshot of feedback keys.
Optional `verify_only: true` performs planning and review without fixing or
repairing. Plans cover every item exactly once, can combine items, and include
per-item expected outcomes and an acyclic dependency graph. Tasks run sequentially
in topological order with selection order breaking ties. Each task and the final
combined patch receive review; a checkpoint permits at most one automatic repair.
Each result has `summary`, `outcomes: [{item,status,evidence}]`, `checks`,
`complete`, and `blocking`. Outcome statuses are `fixed`, `already_addressed`,
`clarify`, and `no_change`; readiness requires complete, nonblocking verification.

To resume, provide `batch` instead of `items`. Optional controls are `adopt: true`
for explicitly inspected manual/partial changes, `replan: true` for unfinished
or changed discussion, `skip: [TASK_ID,…]`, and `trusted_snapshot` from an exact
`trust_required` error. That error also carries `paths` and `diff` for inspection.
Resume keeps the original baseline and provider settings; changed HEAD requires
reconciliation and a new batch. Unstaging code is a human operation. Integrity
failures persist across retries until explicit adoption; records are retained.

`owner-fix` emits standard `started`/`progress` events. Use shared `cancel` with
the `operation_id` from `started`. Both adapters share process-group cancellation,
owned operation records, model limits, and retained artifacts. Execution uses
the owner lock; publication/thread mutations then take the shared PR lock before
short state transactions. Conflicts return `busy`. Human confirmation is outside
all locks.

Without explicit commits, `owner-associate` checks ready batches for a sole-parent,
exact-tree commit. Explicit associations require `batch` and nonempty `commits`
(SHAs or commit revisions); optional `items` limits verified fixed outcomes.
No filename heuristic or fixed-in claim is inferred for other outcomes. Drafts
are generated only after associated commits are reachable from the freshly
fetched PR head. Each original destination receives its own editable draft:
inline thread reply, or source-linked issue/timeline comment for general feedback.

Publication tokens cover exact reply bodies, destinations, commit associations,
viewer, and PR head. Preparation and sending revalidate context/reachability;
every send records durable intent first. Only confirmed receipts become Published.
`owner-recover` looks up markers through complete paginated read-back and never
blindly resends. A proven unsent remainder becomes Unstaged. Edits or changed
dependencies unstage replies; human wording and prior receipts remain intact.
Publishing never resolves threads. The CLI equivalent is `pr-review owner`; see
the [usage examples](../README.md#pr-owner-workflow).

## Reviewer local mutations

`preview-comment` validates an explicit attachment against the current generation
and revision. It returns `{review,comment}` with one temporary comment marked
`composing: true`. The review's revision remains the saved revision. That comment
ID is only for rendering; it cannot be edited, staged, or published through the
API. Cancelling drops the preview locally. Saving uses `mutate/create` with the
chosen title, body, and placement, which returns a new persistent ID. The Emacs
composer requires nonempty text before saving and keeps the input after errors.

Every mutation includes `session`, `generation`, and the last read `revision`.
Stale requests fail without applying changes. The result returns a fresh
`review`, `changed` IDs, and `skipped` IDs/reasons. Store the returned revision.

Use `command: "mutate"` and one of these `operation` values:

| Operation | Fields |
| --- | --- |
| `stage`, `unstage`, `suppress`, `restore` | `comment` or `comments` array; `bulk` for bulk staging |
| `edit` | `comment`, `body` |
| `placement` | `comment`, `placement` object |
| `automatic` | `comment`; restore the model's recommendation |
| `acknowledge` | `comment`; clear the need to inspect changed discussion |
| `create` | `title`, `body`, optional `placement` |
| `verdict` | `verdict`: COMMENT / REQUEST_CHANGES / APPROVE |
| `recover` | `source_generation`, `source_comment`, optional saved `version` index or `document` name; optional destination `comment` |

Placement is one of:

```json
{"kind":"inline","path":"src/file.py","side":"RIGHT","start_line":12,"line":15}
{"kind":"reply","key":"thread:opaque-thread-key"}
{"kind":"review"}
```

Inline paths use the new path for renames; LEFT lines refer to deleted base
lines and RIGHT lines to head additions/context. A complete range must fit one
publishable hunk. Thread placement resolves the exact saved thread/root ID.
Choosing an overall assessment is explicit. There is no guessed/fallback anchor.

Changing wording or placement saves a text/placement version and unstages the
whole draft. Unchanged wording preserves staging. Published comments cannot be
edited; a new comment is an explicit follow-up. Historical generations are
read-only. Recovery copies text/provenance into an Unstaged current draft and
does not change the original. Legacy combined documents are never auto-split.

Mutations use per-generation locks and atomic JSON replacement. A model run's
per-PR lock does not prevent editing its previous completed generation. When a
replacement becomes current, a stale editor retains its buffer text and must
save it explicitly into a current draft.

## Explicit network and model operations

- `refresh`: fetch complete GitHub context, record last checked head/time,
  retain manual text/placement, and unstage affected comments if discussion
  dependencies changed. Replies depend on their thread and referenced discussions;
  inline/overall drafts retain conservative review-wide content dependencies.
  Unrelated resolution or review-status metadata does not unstage drafts. No LLM
  calls or automatic rewriting.
- `run`: run/resume with `session` or `cwd` plus `pr` (number or URL), optional
  `repository`, `reviewers` (nonempty array containing `claude`, `codex`, or both),
  `processing_provider`, `claude_model`, `codex_model`, `claude_effort`,
  `codex_effort`, and `synthesis_tests` (`focused` or `full`). Missing settings come
  from the pending/current generation, then backend defaults. With one reviewer,
  its provider is always the processor. With both, the selected processor handles
  synthesis and discussion matching, using its selected model/effort. The same
  provider handles reassessment and adaptation. Always runs with `--no-publish`.
  An optional `catalogs` object, keyed by provider and containing the `models`
  responses below, validates menu selections before starting work. Testing limits
  are model instructions, not enforced timers. Both independent reviews always
  use focused checks; `synthesis_tests=full` permits a suite only in synthesis
  (or the single-provider structured review). Freeze the complete settings for
  confirmation retries; do not re-read global frontend defaults.
  Without a `session` in the request, completion returns `{sessions: [...]}` with
  the same descriptors as `discover`, scoped to the resolved repository and PR.
  With a `session`, completion returns `{session, generation, new_review_ready}`.
  Store the returned session before reading results; completion does not itself
  replace the displayed generation.
  Both response forms include `status` and `new_review_ready`. If discussion is
  incomplete or reassessment remains unresolved, `status` is `paused`, readiness
  is false, and `pending_generation` identifies the retained unfinished analysis.
  The ordinary `generation` and discovery descriptors still select the current
  review; an unpromoted replacement is never advertised as the current result.
- `cancel`: takes only `operation_id` from a `started` event; verifies process
  identity/start time before signalling the owned operation. The CLI cleans up
  its model process groups and preserves completed checkpoints.
- `adapt`: takes a current draft `comment` and its generation/revision. Uses
  the full attached conversation and finding to propose wording. Returns `body`
  and the original revision, **without saving it**. Accept via a later `edit`
  using that revision. A stale acceptance leaves the suggestion available.

Long operations emit `started` with an operation ID, a `log_path`, and periodic
`progress`. The log retains the progress output even if startup fails before a
review directory exists. Captured model response bodies are not progress output.
An `operation_failed` error includes the CLI's explicit error message when
available, the exit code, operation ID, and saved log path.
Silence from a model does not mean it crashed. Quitting a view is independent
of cancelling a model operation. Pending attempts are durable; a completed
generation is installed only through the backend's atomic current pointer.

Agent configuration requiring trust emits a progress line containing
`confirmation_required: "agent_configuration"`, changed paths, and a
`snapshot` token. After explicit user confirmation, rerun with
`trusted_snapshot` equal to that exact `HEAD:MERGE_BASE` token. A changed source
snapshot requires new confirmation; no faked TTY or automatically piped approval
is used.

## Confirmed publication

1. `prepare` with the current generation/revision resolves old uncertain posts,
   refreshes context, validates Git snapshot/account/placements, and returns
   `ready`, a fresh `review`, counts, verdict, and a `confirmation` token.
2. If not ready, show the reason and affected Unstaged comments. If ready,
   inspect the Staged view, display any returned `warnings` (including retained
   blockers when approving), and ask for a brief final confirmation.
3. `publish` includes the returned review revision and confirmation token.
   Any intervening local mutation invalidates confirmation. Source/discussion
   are revalidated before and between writes.
4. Render the returned review. `complete: false` may indicate partial success;
   confirmed comments are Published and unsent comments remain Staged unless
   fresh discussion requires inspection. `resolve` explicitly reconciles
   ambiguous outcomes before preparing another batch.

Each draft becomes one comment. New inline comments use the GitHub review
`comments` array; replies use the root-comment reply endpoint. There is at most
one explicitly written overall assessment in a review. An explicitly chosen
verdict may be submitted without comments. Replies-only batches have no new
review verdict. Hidden markers accompany exact saved human wording.

For a newly imported generation, the initial verdict is COMMENT. APPROVE and
REQUEST_CHANGES require explicit `verdict` selection; legacy automatic defaults
are interpreted as COMMENT. Existing explicit choices and in-flight publication
receipts are preserved. This is one overall PR assessment;
individual comments do not have separate verdicts.

## Conversations and limited follow-up

Capabilities advertise `conversations`, `review-status`, and `followup`. `read`
adds `coverage: {kind: "full"|"followup", threads: [...]}`, `review_status`, and
`followup_assessments`. Legacy generations default to full coverage. A follow-up
has `recommendation: null`; it makes no overall model recommendation. Each history
entry includes `analysis_kind`.

`review_status` contains `available`, `checked_at`, GitHub's aggregate review
`decision`, the viewer's latest `opinionated` review, and `latest_activity`.
Review objects retain GitHub's `id`, `state`, `submittedAt`, `url`, and `commit.oid`.
Missing optional status support means unavailable, not approved. This metadata is
separate from discussion fingerprints and is not overall merge eligibility.

Thread records add nullable `can_resolve`, `can_unresolve`, and `resolved_by`.
Each `thread_contexts` entry includes `participated` and an `expected` token object.
Keep `is_outdated` and `is_resolved` separate.

`thread-state` takes the ordinary session/generation/revision fields, `thread`
(a saved thread key), `resolved` (boolean), and the displayed context's `expected`
object. This explicit command performs one direct network operation; it is not a
draft mutation or staged publication. It checks the authenticated account, PR
membership/open state, permissions, current head, and thread contents. Changed
context returns a refreshed `review` and a reason without writing. An already
satisfied state is a no-op. Responses contain `changed`, `reason`, `review`, and
optionally `uncertain`; read-back after a lost response reports observed state
without claiming authorship or blindly retrying. The existing `resolve` command
continues to mean publication-receipt recovery.

`followup` takes session/generation/revision and a nonempty `threads` array of
distinct saved thread keys. Optional model settings and `trusted_snapshot` follow
`run` conventions. It refreshes GitHub, pins the latest source, invokes only the
saved processing provider, and creates a limited generation through the existing
attempt/history machinery. Focused verification is always used. Like `run`, it
emits operation progress, supports cancellation, and returns the new generation
without changing the displayed view. Repeating unchanged inputs reuses results.

Each assessment has `thread_key`, `outcome` (ADDRESSED, STILL_PRESENT,
NOT_APPLICABLE, or UNCERTAIN), `evidence`, `limitations`, and `suggested_reply`
(possibly empty). Read views add `head` and `stale`. No draft is created
automatically. Clients can explicitly `create` an attached reply with a fresh
suggestion and then use the existing edit/stage/prepare/publish flow.

Follow-up preserves prior generations and starts with no carried drafts. An
explicit overall verdict is permitted, but `prepare.warnings` discloses limited
coverage. Review-status changes invalidate confirmation without unnecessarily
unstaging drafts. Neither assessment nor thread resolution clears a review verdict.

Intent is durable before each network request. Parent review and individual
inline comment receipts are retained, and lookup matches marker, viewer,
commit/thread, and event. A known parent review whose inline receipts are not
yet available remains unresolved and is never reposted. Confirmed own replies,
their implicit COMMENTED review wrappers, and own inline comments are excluded
from external-discussion freshness comparisons.

The API and terminal call the shared Python application services directly.
Protocol 1 remains the Emacs boundary; internal services do not serialize API
requests or invoke the CLI recursively. Both interfaces always use the same
draft and publication ledger. Legacy action ledgers are read-only import sources.
All model-generated staging, grouping, or wording changes happen before user
staging, never as a side effect of sending an approved batch.

## Errors and compatibility

Important codes include `protocol_mismatch`, `stale_revision`, `historical`,
`busy`, `invalid_anchor`, `invalid_target`, `not_ready`, `confirmation_expired`,
`ambiguous_publication`, `incomplete_receipts`, and `cancelled`.

Clients should retain unsaved text on every error, display the backend's message,
and offer refresh/recovery. Never retry a write blindly after an unknown outcome.
Unknown protocol versions fail explicitly. Existing CLI review artifacts remain
readable without migration or paid calls. New model outputs include location
fields (an empty list and null anchor when unknown); legacy outputs may omit them.

Implementation: `lib/pr_review/api.py` frames NDJSON; `application.py` dispatches
shared operations; `reviews.py` and `pipeline.py` own generation; `processes.py`
owns subprocess groups and cancellation. Local draft operations acquire only their
generation lock. Operations needing both locks acquire the PR lock first, then the
draft lock. Promotion waits for an in-progress save before switching the pointer.
CLI explicit external resume is an internal capability and is never accepted from
an API request field; protocol sessions remain restricted to the state directory.
