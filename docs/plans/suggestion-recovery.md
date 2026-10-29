# Suggestion recovery and review coverage

## Resume here

Read repository instructions, this plan, `git status`, and recent commits before
continuing. Update this file with each work package. Do not depend on chat history
or temporary artifacts. Current step: implement package 2.

The pre-existing owner assessment/resume repair is committed separately as
`b016b30`. Validation: `make lint test-api`, 335 Python tests passed. It separates
completed rejection from unfinished verification and preserves repair budgets.

## Scope and decisions

Investigation baseline: PR 8, commit `5143269`. Both saved full reviews used the
same 26-file diff. The dual-provider review retained twelve findings; the
Codex-only review returned one before suggestion generation. No findings were
lost downstream. The single-provider run also found an Emacs editing defect
absent from the dual-provider report. Finding IDs below are synthesis IDs, not
draft comment IDs. More findings do not necessarily mean better findings.

- Preserve all-or-nothing automatic publication: any unresolved finding blocks
  the batch. Never silently drop a finding because its suggestion is unsuitable.
- Permit deliberate interactive selection of individually accepted suggestions.
- Retain analyses, generated attempts, and history. Keep human decisions distinct
  from machine-generated history; unknown legacy authorship remains conservative.
- Verification remains bound to exact source, prose, replacement, and settings.
- Do not reset owner repair limits, change publication verdict defaults, run paid
  model reviews, or publish to GitHub as part of this implementation.
- Each package: reproduce with regression tests, implement, run relevant checks,
  update this plan, commit. Run the full available check suite at the end.

## Work packages

### 1. Generation history and failed candidates — F1, F3

Status: complete. Generated history is tagged automatic and remains in recoverable
versions; legacy/unknown and manual history still stop auto publication. Candidates
are validated before attachment. Failed/pending legacy candidates retry on resume.
Validation: all 74 suggestion/automation tests passed, including three new
regressions (failed/pending legacy states are separate subcases).

Reproduction: regenerating an untouched suggestion appends to `versions`, which
the automatic guard mistakes for manual editing. A generated candidate rejected
by validation remains attached with a matching fingerprint; resume skips it.

Acceptance: generated history remains recoverable without falsely blocking auto
mode; human/unknown history still blocks it. Invalid candidates do not move the
finding's placement; a later resume retries generation, including saved legacy
failed candidates. Successful generation/verification is reused.

### 2. Interactive subsets and truthful verification display — F4, F11

Status: pending. Depends on package 1 only for shared generation tests.

Reproduction: accepted A+B and rejected C never get a combined A+B check. A
changed suggestion displays an old batch's accepted result despite blockers.

Acceptance: verify the individually accepted subset together; C continues to
block automatic publication, but interactive staging of A+B works. Display batch
verification only for the exact current suggestion identity included in it.
Overlapping or jointly rejected suggestions still cannot publish together.

### 3. Cancellation preserves staging — F2

Status: pending.

Reproduction: `Cancelled` is an `ApiError`; suggestion blockers catch it, causing
publication preparation to unstage and save a comment before cancellation exits.

Acceptance: cancellation during validation propagates immediately and does not
turn into a saved validation failure or change the selected publication batch.

### 4. Coherent historical recovery — F7

Status: pending. Establish and document semantics before implementation.

Reproduction: recovering a pre-suggestion version removes the replacement but
leaves its moved anchor. Recovering one comment into another overwrites finding
IDs while retaining the destination title/evidence.

Acceptance: same-comment historical recovery restores coherent placement and
suggestion state. Recovery into another existing draft preserves its identity;
it must not silently transplant a replacement for another finding. A fresh
recovered draft retains the source suggestion context. All restored suggestions
require fresh verification. Ordinary text/document recovery remains supported.

### 5. Markdown edge cases — F5

Status: pending.

Reproduction: validation accepts unclosed `<?`, `<!DOCTYPE`, and `<![CDATA[` HTML
blocks, which can consume the appended fence under GFM parsing rules. Live GitHub
rendering was not tested. Reference: https://github.github.com/gfm/#html-blocks.

Acceptance: reject prose that can absorb the generated fence; allow ordinary
closed code examples. Use focused rendering/validation cases without posting.

### 6. Review coverage and test environment

Status: pending investigation. Independent of packages 1–5.

The single-provider report did not report six reproducible functional/UI defects
(F1, F2, F3, F4, F7, F11). F5 has code/specification support. Its suite stopped at
bytecode creation in a read-only checkout, and its Emacs fixture lacked Magit.

Investigate practical coverage instructions for failure/save/resume,
edit/invalidate/verify, selection/staging/publication, and recovery. No finding
quota. Determine how documented tests can run without writing into the reviewed
checkout; preserve sandbox restrictions and disclose unavailable dependencies.
Record implemented improvements, remaining limitations, and validation. Do not
claim improved model recall without a later controlled dogfood run.

## Deferred

- F6: stricter placement-edit policy; verification already becomes invalid.
- F8: memoization; two displayed suggestions caused four `git show` and four
  `git ls-tree` calls, but material latency was not measured.
- F9: clone reuse; fresh isolation has a purpose.
- F10: shared provider parsing/diagnostics; raw outputs are already retained.
- F12: exact-set-only verification; current verifier explicitly requires subset
  independence, so no concrete correctness failure was established.

## Validation and completion log

- Initial investigation used isolated Git fixtures and mocked model/network
  responses. Convert the relevant reproductions into repository tests per package.
- Existing owner repair: `b016b30`, lint and all 335 Python tests passed.
