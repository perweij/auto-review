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
| `read` | `session`, optional `generation` | Saved review, comments, contextual rows/threads, staging, last check, operations |
| `history` | `session` | Generations, commits, times, status, saved edits/document counts |
| `diff` | `session`, `generation`, optional `path` | Reviewed changed files, hunks, and patches |
| `file` | `session`, `generation`, `path`, optional `side`, `commit` | Immutable Git blob text and revision |

Omitting `generation` in `read` selects the completed current generation. Pin
the returned identity for subsequent reads; do not silently follow a pointer.
The response separately identifies `current_generation`, `pending_generation`,
and verified running operations. Explicit `g` in the main UI selects current;
history buffers keep reading their pinned generation until the user chooses
`N` to open current. Background completion never replaces the displayed text,
and an open inline editor prevents structural refresh.

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

## Local mutations

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
  changed. No LLM calls or automatic rewriting.
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
  are model instructions, not enforced timers. Freeze the complete settings for
  confirmation retries; do not re-read global frontend defaults.
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

For a newly imported generation, the initial verdict follows the model's
recommendation when permitted for the authenticated account, otherwise COMMENT.
Existing saved verdict choices are preserved. This is one overall PR assessment;
individual comments do not have separate verdicts.

Intent is durable before each network request. Parent review and individual
inline comment receipts are retained, and lookup matches marker, viewer,
commit/thread, and event. A known parent review whose inline receipts are not
yet available remains unresolved and is never reposted. Confirmed own replies,
their implicit COMMENTED review wrappers, and own inline comments are excluded
from external-discussion freshness comparisons.

The API and terminal share publication receipts. After API draft adoption,
the terminal uses the API draft ledger rather than an obsolete aggregate body.
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
