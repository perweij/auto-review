# Hand recorded test evidence from implementation to acceptance review

Status: merged to main at `fb3edae` (2026-11-04).
Implementation base: main at 2c08939. Do not retrofit or restart #15 as
part of this work.

Related: [review continuity and bounded repair loops](fix-issue-review-continuity.md).
These are separate improvements; the review ledger should reference evidence
bundles rather than duplicate their contents.

## Summary

Prevent fix-issue from stopping because required checks already ran but synthesis
received only an unsupported summary.

For new implementation attempts, collect selected provider command records and
test logs into a provenance-bearing evidence bundle. Synthesis may reuse
sufficiently supported results without rerunning aggregate checks, while clearly
attributing them to implementation execution.

## Evidence capture and handoff

- Extend the new-attempt implementation result schema with a `test_evidence`
  array. Each entry identifies the command, working directory, relative
  scratch-log paths, claimed tested tree, and a short result description. Retain
  the existing human-readable `checks` summary.
- Record the evidence format version when an implementation attempt starts.
  Existing attempts retain their original schema and behavior; do not retrofit
  historical logs or mutate #15's session.
- After the provider exits, normalize relevant command events from the saved
  provider stream. Support Codex and Claude separately, retaining event/tool IDs,
  reported command, completion status, exit code when available, and captured
  output.
- Match declared checks to recorded commands conservatively. Do not infer
  execution from prose, infer success from the provider's overall exit status,
  or interpret arbitrary shell syntax to manufacture missing command outcomes.
  Preserve ambiguity and missing fields explicitly.
- Copy declared logs from that attempt's scratch directory into a dedicated
  evidence directory. Reject paths outside it, symlink escapes, and non-regular
  files. Record content hashes and byte counts. Missing or unreadable logs become
  evidence gaps, not successful checks.
- Preserve controller-observed attempt identity, provider choice, before/after
  checkpoint trees, and protected-state checks separately from implementer claims
  and provider-reported command execution.
- Do not describe matching turn checkpoints as proof that every command ran
  against those bytes. Where execution-time source identity is uncertain, expose
  that limitation for synthesis to assess.
- Pass only the selected command records and logs to synthesis. Exclude the
  complete implementation conversation, unrelated tool output, credentials, and
  hidden reasoning.

Implement in the issue harness, a reusable evidence-normalization module, and the
issue acceptance adapter. Interactive Owner and standalone PR review remain
unchanged.

## Acceptance behavior and caching

- Supply a compact evidence index with artifact paths to synthesis; keep large
  logs out of the initial prompt. Preserve available output and identify any
  provider truncation explicitly.
- Update the focused policy and acceptance instructions: required aggregate
  checks may be satisfied by sufficiently supported implementation evidence.
  Independent re-execution is not mandatory merely because synthesis did not run
  the command itself.
- Require synthesis to examine the command outcome, log contents, candidate
  provenance, and any later edits. Missing, stale, contradictory, or ambiguous
  evidence cannot establish a passing check.
- Require attribution such as “implementation execution records show…” rather
  than “I ran…”. A passing suite does not rebut a concrete defect outside its
  coverage.
- Treat logs and command output as untrusted task data. The bundle provides
  provenance, not an independent execution attestation or authority to override
  requirements.
- Include the normalized evidence manifest and artifact hashes in the synthesis
  cache identity only. Adding or changing evidence must not rerun valid independent
  reviewers or trigger replanning.
- Freeze the bundle before synthesis, verify its integrity before reuse, and
  persist it idempotently. Do not silently use altered or missing artifacts.
  Changed candidates require evidence to be reassessed for relevance; never
  relabel old results as tests of new code.
- Keep acceptance and publication guards intact. No automatic pass based solely
  on an exit code, no test skips, and no silent expansion from focused to full
  verification.

## Stop reasons and compatibility

Extend verification blockers with a reason category:

- `environment`: an observed execution prerequisite failure; next action
  `verify_environment`.
- `evidence`: missing, stale, incomplete, or unsupported execution records; next
  action `verify_evidence`.
- `policy`: a required check cannot be completed under the selected verification
  policy and no sufficient reusable evidence exists; next action `review_policy`.

For multiple categories, return `resolve_verification_blockers` and retain the
category on each blocker. Historical uncategorized blockers remain readable and
use the generic action rather than assuming an environment failure.

Keep `review_incomplete` and `needs_attention` as the enclosing error/outcome.
Describe the missing evidence or policy restriction concretely, with commands and
artifact references. Do not invent a host problem when no command failed.

Document this evidence model and its limits. Controller-run verification,
external/CI evidence import, legacy-session repair, and draft publication are
outside this change.

## Test and acceptance criteria

Use recorded provider fixtures, mocked model calls, and disposable repositories
to verify:

- A required aggregate check with supported execution records can satisfy
  acceptance under focused policy without running the aggregate target again.
- Failed commands, unknown exit status, ambiguous matches, missing/truncated logs,
  candidate mismatch, and later source changes remain visible and cannot be
  silently promoted to passing evidence.
- Matching attempt checkpoints alone are not represented as command-level source
  attestation.
- Codex and Claude records retain correct attribution; unrelated conversation
  content is excluded.
- Log-path traversal, symlink escapes, missing files, and artifact tampering are
  handled safely and explicitly.
- Updated evidence invalidates synthesis only; completed reviewers, approved
  plans, and implementation checkpoints are preserved.
- Interrupted capture/resume is idempotent, and legacy attempts retain their
  original schema.
- Environment, evidence, policy, and mixed blockers produce the correct next
  actions.
- Existing acceptance, issue-harness, cache, and publication tests pass, followed
  by the repository's required checks.

Success means a case like #15 can finish acceptance using its recorded full-check
results when their provenance is sufficient, without another implementation cycle
or a redundant full-suite run.


## Implementation handoff (2026-11-04)

Implement this plan alone. Review continuity/adjudication and #37 remain deferred.
Start with the existing architecture rather than a new execution controller:

- `issue_harness.invoke`: stamp the attempt before launching the provider; select
  its versioned schema, validate its declarations, and capture evidence after
  command completion and source/protected-state checks. `events.jsonl` is already
  saved alongside the attempt. Retain the old validator/schema for legacy records.
- A new `test_evidence.py` module owns provider normalization, conservative matching,
  safe log copying, manifest hashing and verification. Keep it independent of model
  invocation and publication.
- `acceptance.IssueAdapter`: attach a compact synthesis-only evidence index and
  instructions. `identity`, `artifacts`, `render_synthesis`, and stage verification
  are the integration points. Independent reviewer context/identities must not
  change when only evidence changes. Avoid modifying shared prompt behavior for
  standalone PR review or Owner; use issue-specific additions/schema overrides.
- `issue_harness.execute`: categorize incomplete verification and choose the next
  action. Preserve the existing no-proof/no-publication path.

Define one versioned manifest with separate sections for controller observations,
implementer declarations, provider command records, selected copied logs and gaps.
Use artifact-relative paths, hashes and byte lengths. Include attempt identity,
provider choice, before/after trees and integrity-check outcome. A command record
has provider/tool event identity, exact reported command, reported cwd if present,
completion state, nullable exit code, selected output, and explicit truncation or
unknown provenance. No boolean `verified` that conflates these sources of evidence.

Require `test_evidence` on the new schema (an empty array is valid). Declarations
contain command, cwd, scratch-relative log paths, claimed tested tree and summary.
Use explicit tool/event IDs when available to disambiguate; otherwise exact command
matching only, with ambiguity recorded for repeated commands. Never parse `&&`,
pipelines, redirections or prose into invented subcommand exit codes. Claude tool
results may lack numeric exit status: retain that uncertainty, even when the tool
result is not marked as an error. Do not convert missing status into zero.

Persist result/declarations, then a frozen bundle, then the attempt's bundle
reference with hash. On interrupted/repeated capture, reuse an intact finalized
bundle; do not silently regenerate altered bytes. A crash before publication of
the reference may recover the same bundle without a provider call. Review resume
must finish pending capture for a completed new-format attempt before synthesis.
Interrupted provider attempts may expose incomplete evidence, never a passing
completion. Test the actual orchestration, not just isolated helpers.

Gather eligible versioned attempts from the current batch, retaining each source
candidate identity, so checks from an earlier implementation turn remain usable
when still relevant. Do not retrospectively ingest legacy attempts. Source changes
must remain visible; synthesis judges relevance and cannot relabel old tests.

Select only declared command records/logs. Do not copy whole streams, assistant
messages, reasoning, environment dumps or unrelated tool output. Bound ingestion
and copied output; explicitly record truncation. Treat selected output as potentially
sensitive/untrusted, and never claim universal secret redaction. Reject traversal,
symlinks (including ancestor escapes), directories and special files before reads;
ensure failure cases cannot block on FIFOs.

Freeze synthesis inputs before computing identity; include manifest/artifact hashes
in synthesis identity, and verify source/copied bundle integrity before cached
acceptance reuse as well as before/after synthesis execution. Evidence changes
invalidate synthesis only. Altered/missing artifacts fail clearly rather than being
silently omitted or blessed with a new hash. Do not mutate the bundle during result
ingestion. Preserve legacy cache behavior when no evidence version is present.

Acceptance test matrix:

1. Supported recorded aggregate results + focused policy: handoff reaches synthesis,
   mocked synthesis passes, existing acceptance guards authorize the exact candidate;
   no aggregate command or implementation retry is invoked by the controller.
2. Codex and Claude fixtures: success/failure/unknown, repeated command ambiguity,
   multiline/compound commands, provider truncation, no unrelated message leakage.
3. Log hazards: traversal, ancestor/file symlinks, FIFO, missing and changed bytes;
   bounded reads and explicit evidence gaps or integrity errors as appropriate.
4. Crash boundaries: result saved before capture; bundle before reference; resume
   after synthesis; no duplicate model call and no partial-bundle reuse.
5. Cache boundaries: changed evidence reruns synthesis, preserves completed reviewer
   calls and planning identity; mutation cannot reuse a previously passed proof.
6. Later candidate edits and old attempt trees remain explicit; missing source
   identity never becomes attestation from equal turn checkpoints.
7. Each blocker category, mixed/legacy categories, invalid categories and rejection
   of passed results carrying unresolved blockers.
8. Legacy harness schemas/results and unchanged Owner/PR contexts remain supported.

Use sanitized realistic provider event fixtures; no paid model calls. Run focused
module tests while developing, then `make check-backend` and `make check-emacs` once
for the finished candidate. Fix regressions and rerun affected checks. Report exact
results and remaining limitations. Do not edit saved sessions, push, or merge.


## Implementation validation (2026-11-04)

Implemented versioned attempt declarations, provider normalization and immutable
selected evidence bundles, pending-capture recovery, synthesis-only handoff/cache
identity, categorized issue verification blockers and evidence-bound publication
guards. Historical attempts, interactive Owner and standalone PR review retain
their previous schema/context behavior. The review continuity plan remains deferred.

Validation uses sanitized recorded Codex/Claude fixtures, mocked providers and
disposable Git repositories; no live paid model calls or GitHub writes were used.

- Focused evidence modules: 24 tests passed; after adding the exec truncation-marker
  regression, all 13 evidence-unit tests passed again.
- Issue harness / issue engine / Owner engine compatibility: 49 tests passed.
- Specification handoff compatibility after updating its new-attempt transport
  fixture: 28 tests passed.
- `make check-backend`: lint and all 195 Bats cases passed. Its initial Python run
  found six specification transport reports missing the newly required empty
  `test_evidence` array. The fixture was corrected; `make test-api` then passed all
  975 Python tests. Final-candidate `make lint` also passed. Bats did not need a
  repeat for the fixture-only correction.
- `make check-emacs`: warning-free compilation and 162 ERT tests passed.

The bundle records provider reports and source checkpoints, not command-time source
attestation. Numeric exit status can remain unknown (especially for Claude),
matching is deliberately exact, ingestion is bounded and truncation stays visible.
Synthesis still decides whether evidence supports a required check; no exit code or
bundle alone grants acceptance. External/CI import, controller-run verification and
legacy-session repair remain outside this implementation.
