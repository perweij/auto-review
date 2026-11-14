# Live forge smoke and functional tests

Status: implemented on `feature/glab`. Autonomous results and known
limitations are recorded below; assisted checks remain pending.
Follow-up: https://github.com/perweij/auto-review/issues/39 tracks economical
preset coverage. Production preset changes are outside this work.

## Entry points and cost

- `make smoke`: scripted smoke followed by three real-model economical workflows.
- `make smoke-fast`: scripted smoke only; no model quota.
- `make smoke-extended`: smoke plus extended scripted functional cases; no model quota.

Defaults use sibling `temp-eval-auto-review-github` and
`temp-eval-auto-review-gitlab` checkouts. Advanced runner options select repositories,
scenarios, retained run resume, or explicit cleanup. No studying a manual or
editing configuration should be necessary for the supplied setup. Missing
credentials produce an actionable sourcing instruction. Never source arbitrary
secret files automatically.

## 1. Autonomous implementation and tests

Implement `tests/smoke-forges.py`, standard-library Python, using application CLI
and public JSON protocol for operations under test. Native APIs create fixtures
and independently verify effects. Keep network/model suites outside normal
`make check`; include harness/provider-double contract tests in backend checks.

Modes: smoke (scripted), extended (scripted), live (three paid workflows).
`make smoke` runs smoke then live. Scenario selection includes dependencies.
Retain fixtures by default; resume reconciles existing state, cleanup verifies
ownership before closing run-owned objects and removing run-owned branches.
Never merge or delete projects.

### Isolation and preflight

Verify canonical target identity, API authentication/access, Git fetch/push
transport, tools, and sandbox. Preserve model authentication and Git credential
helpers; isolate XDG_STATE_HOME, fresh clones, and private run artifacts rather
than replacing HOME. Keep supplied checkouts and default branches unchanged.
Credentials stay out of manifests, URLs, arguments, and reports. Model probes
run only in live mode and count as potential usage.

Snapshot tracked and untracked implementation contents without changing the
real index; record commit, snapshot hash, changes, tool versions. Persist a
manifest of fixtures, sessions, remote IDs, and scenario outcomes atomically.
Reconcile orphan creation using unique run identity and application workflow
markers. Resume rejects incompatible implementation/harness snapshots.

### Fixtures and model doubles

Use a tiny standard-library Python calculation fixture, deliberately faulty
PR/MR, populated discussions, and dedicated fixture base branches. A separate
GitHub issue requests a new small module from the README-only default branch.
Scripted provider executables cover probes/discovery, planning, critique,
synthesis, review, and real fixture edits. Offline contract tests detect schema
and invocation drift. Do not fabricate successful application state. Verify
same-account Owner behavior early; fixture text never imitates application
publication markers.

### Smoke scenarios

1. GitHub CLI review/resume: metadata, discussions, checkout SHA/path logs,
   completed work reuse without new model stages.
2. Prepare/publish COMMENT review with inline finding, exact content and receipts,
   repeat without duplicates; scripted auto-publish JSON path separately.
3. Issue eligibility, published plan, approval stop, exact revision approval,
   draft creation, implementation/tests, ready transition, completed-result reuse.
4. Owner open/refresh, compose/publish reply, resolve/reopen, live checkout.
5. Accepted-but-hidden response recovery for reviewer publication and issue
   comment; journaled pre-send failure and inspected absent-write retry.
6. Advance PR head after preparing publication: refuse stale preview and review
   a new generation on resume.
7. Foreign commit on managed issue branch: stop without overwriting/pushing.
8. GitLab read-only review/resume, source SHA, discussion anchors, honest pipeline
   coverage. Unsupported-operation rejection is local capability coverage;
   successful reads and zero mutations establish live coverage.
9. Integrity/output: supplied working copies/default branches unchanged,
   parseable JSON, distinct checkout/artifact paths.

### Extended scenarios

- SIGINT during a model stage, completed-stage reuse on resume.
- Concurrent invocation returns busy without duplicate operations.
- Settings changes at approval boundary without replan/reapproval.
- Draft editing invalidates preview; fresh preview publishes edited content.
- Remote plan edits/deletion stop without duplicate plan.
- Wrong-revision approval, acknowledgment, issue-body change invalidates approval.
- Owner refresh after external replies/resolution; already-ready/closed objects.
- GitLab head advancement and outdated positioned discussions.
- Recovery for Owner reply, PR creation, ready transitions across invocations.
- Emacs batch with real backend: state, shared w, historical checkout selection,
  missing checkout explanation without effects.
- Accounting: missing versus zero, synthetic versus reported usage, reused work.

Defer pagination stress, fork MRs, suggestions, follow-up reviews, complete
Owner fix/commit/push, trust/hooks, and injected quota exhaustion. Report gaps.

### Fault injection and oracles

Scope a forge-command wrapper to one method/endpoint/body. Distinguish GraphQL
reads from mutations. Prove forwarded write success before suppressing its
response with a transport error or empty success; never use rejection HTTP codes
for transport loss. Ready-transition cross-run recovery also fails immediate
readback. Absent-write injection occurs after durable intent creation.

Bound polling for remote visibility; unresolved visibility is blocked, not a
fabricated product failure. Independently count effects with full pagination and
verify exact bodies/receipts/native state. Compare logged checkout SHA to fetched
head. Run implementation tests in a fresh clone of PR head, check diff scope and
plan link. Prove reuse using process traces and absence of new remote mutations.

### Paid phase

One GitHub review, one GitLab review, one GitHub issue workflow. Use economical
settings, and for issue fixing also pass:

```
--planning codex-1 --fix codex-1 --planning-review claude-1
```

Check every resolved role before launch and invocation arguments afterward;
retain choices on resume. No automatic upgrades, paid retries, or more external
model reviews. PR_REVIEW_MODEL_TIMEOUT=600, one issue repair cycle, 60-minute
issue allowance, 90-minute live phase. Interrupt gracefully; retain diagnostics
if escalation needed. Runtime caps do not guarantee monetary cost. Record probes
and available usage without conflating tokens and subscription quota.

### Validation and reporting

Run backend/Emacs baseline, scripted smoke and extended, then economical live
workflows (user explicitly approved model runs in make smoke). Report JSON and
readable pass/fail/blocked/skipped results, mode, snapshot, commands/settings,
timing/usage, URLs/sessions, sanitized diagnostics. Required blocked/fail ->
nonzero; dependents skipped-by-dependency. Document unavailable pipelines/runners
and other gaps. Update this document and GitLab pilot evidence after execution.
Fix harness defects and rerun affected checks; report product bugs separately.
The implementation and validation are recorded in the feature branch commits.

## 2. Assisted tests (last)

Provide fixture URLs, retained session paths, short steps and expected results.
Interactive Emacs: menu discoverability, progress/attention, editing/elaboration,
w opens correct Dired. Second identity if available: PR verdicts and other-user
feedback. Same-account issue-plan approval is intentional autonomous coverage.
Append assisted results to the report; autonomous work does not wait for them.

## Results — 2026-11-14

- Created [issue #39](https://github.com/perweij/auto-review/issues/39) for
  economical issue-role defaults. The suite explicitly selects economical
  planning, critique and fix roles without changing production presets.
- Baseline passed: 1231 Python tests, 201 Bats tests and 172 Emacs tests.
  Targeted verification passed: nine harness tests and 17 planning tests.
- Scripted core suite passed against both live forges:
  `/tmp/auto-review-forge-smoke-1wcmwg4v/report.md`. This covers review/reuse,
  publication and uncertain-write recovery, Owner replies and resolution, issue
  approval/fixing/ready, independent execution of the resulting tests, completed
  reuse, foreign-commit refusal and default-branch integrity.
- Extended scenarios are implemented. The full development run at
  `/tmp/auto-review-forge-smoke-7t0_zytw/report.md` passed recovery, interruption,
  concurrency, changed approval, settings handoff, accounting and outdated GitLab
  discussion checks. Two harness assumptions needed correction: edited plans can
  produce a fresh revision awaiting approval; historical views without a retained
  checkout must report unavailable. Corrected edited-plan verification passed in
  `state/forge-smoke/run-e65_rf1_/report.md`; corrected Emacs checkout verification
  passed in `state/forge-smoke/run-48hd8nun/report.md`. The former run also encountered
  a transient GitHub API timeout during approval verification. These are combined
  results from a full run and targeted rechecks, not one all-green extended run.
- Real economical GitHub and GitLab reviews, including resume, passed:
  `/tmp/auto-review-forge-smoke-93n88ef4/report.md`.
- Real economical issue execution failed at planning synthesis after draft and
  critique completed: Codex returned `no rollout found for thread id` on resume.
  Evidence: `/tmp/auto-review-forge-smoke-x02g9v3q/report.md` and its planning
  `stderr-0.log`. The storage cause was subsequently identified and repaired; see the repair
  section below. No automatic paid retry or model upgrade was attempted during
  the initial suite. End-to-end real-model issue completion is **not verified**.
- Functional execution exposed and repaired a narrow production defect:
  `planning_review.assess` must accept the `(stream, line)` callback used by
  `processes.run`. The regression test now uses that calling convention and checks
  that stderr events are ignored. Other product failures remain recorded findings.
- Supplied working copies and default branches remained unchanged in integrity
  sweeps. Fixtures and artifacts remain available for the assisted checks above.
  Fork MRs, pipeline execution, interactive Emacs UX and second-account verdicts
  remain outside the completed autonomous evidence.

Additional final checks:

- Automatic CLI JSON publication and repeat/no-duplicate behavior passed, as did
  changed-issue approval invalidation: `state/forge-smoke/run-7sv5o5xk/report.md`.
- Deleted-plan receipt recovery passed after correcting the harness: a missing
  previously receipted comment is `changed`, not safely `absent`. The application
  stops without republishing. Evidence: `state/forge-smoke/run-56wxxtrc/report.md`.
  The earlier failed assertion remains in the recheck report for provenance.
- Native fixture cleanup and a second idempotent cleanup passed on GitHub and
  GitLab: `state/forge-smoke/run-k55x1sj8/report.md`. Other retained fixtures were
  left available for assisted inspection.
- Reports, manifests, source snapshots and command logs from the four principal
  `/tmp` runs were copied to ignored `state/forge-smoke/evidence-*` directories.
  Original session/worktree locations remain unchanged; evidence copies are not
  resumable runs and exclude provider homes.

## Quick use

From the auto-review checkout, load the supplied credentials once in your shell:

```sh
. ../.secret_gh
. ../.secret-glab
make smoke-fast
```

Use `make smoke` for scripted checks followed by real economical models (uses
quota); `make smoke-extended` selects additional scripted cases. Every run prints
its report, resume command, and explicit cleanup command. Fixtures stay available
for inspection by default. No publishing/approval prompts are needed: approvals
apply only to the suite's deliberately created issue fixtures.

The initial real-model issue failure is retained for diagnosis. The targeted
planning-session repair below passed live verification; the entire real-model
issue workflow has not yet been rerun through fixing and ready. Reports distinguish
failed, blocked and skipped checks from passing ones.

## Planning session storage repair

The real-model failure above exposed absolute session-index paths into a deleted
planning workspace. The copied transcript existed, but the copied Codex database
still referenced its original temporary location.

Regression first: a provider double writes an absolute transcript path, interrupts
the workflow during critique, then attempts synthesis after the draft checkout has
been removed. It failed with `FileNotFoundError` before the repair.

Implementation contract:

- Give each planner conversation a stable private provider home. Initialize it
  once from the isolated provider configuration; invoke directly against that home.
- Continue creating and verifying disposable pinned source checkouts per stage.
  Critics retain independent temporary homes without planner transcripts.
- Keep provider storage in place across errors and later invocations. Do not
  rewrite provider databases or copy active session indexes between paths.
- For legacy copied homes, retain completed draft/critique evidence and start a
  fresh provider conversation using the supplied artifacts. Preserve the legacy
  journal entry for diagnosis; do not discard completed planning results.

The regression and legacy migration check pass after implementation, along with
48 planning, isolation-cleanup and issue-harness tests. A focused economical
real-model synthesis check reuses the previously failed fixture's draft/critique;
results:

- Legacy failed fixture recovered to `outcome=approval`, exit 2 (expected user
  approval boundary), with draft and critique reused. Evidence:
  `state/forge-smoke/stable-home-live.json` and `.log`.
- Explicit synthesis regeneration resumed the same new Codex session ID after
  checkout cleanup and again reached approval. Its database transcript path
  exists. Evidence: `state/forge-smoke/stable-home-resume.json` and `.log`.
- Draft and critique call counts remained one each. Only two new economical Terra
  synthesis calls were needed: legacy recovery and the actual resume check.
- All 22 provider/synthesis-policy Bats tests passed. After strengthening the
  regression to use the same provider for the independent critic, all 21 planning
  and isolation-cleanup tests passed again.

This verifies planning recovery and session resumption, not the remainder of the
real-model issue-fixing workflow. The storage repair is committed as `c43bd8d`.
