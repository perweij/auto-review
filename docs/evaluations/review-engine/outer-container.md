# Outer-container execution validation (#30)

Recorded 2026-11-02, after PR #28 merged. The original sandbox limitation and
not-run PR4 record remain unchanged in this directory.

## Provider execution

Claude Code 2.1.316 and Codex CLI 0.166.0 were tested inside the deployment container.
All Claude command builders request `sandbox.enabled=false`, including metadata
requests which retain `disableAllHooks=true`. Codex execution requests
`sandbox_mode=danger-full-access` and `approval_policy=never`. Codex app-server
model discovery remains metadata-only.

`tests/smoke-provider-policy.py --provider claude --model haiku` and the same command
with `--provider codex --model gpt-6-luna` passed. Each uses the production issue
implementation command builder twice: a fresh session, then a resumed turn. Each
turn reads a newly generated token from the candidate and executes a shell command
which creates a checked marker. Neither run invoked the failing `bwrap` executable
placed first on PATH. Effective argv and results are recorded in
[outer-container-live.json](results/outer-container-live.json); raw local artifacts
are named there. This records requested settings and observed execution, not a way
to override provider-managed policy.

The first draft of this smoke prompt ambiguously asked for “the file contents”
after creating a second file. Both providers executed the read and shell command,
but the response assertion failed: Claude added prose and Codex returned the
second file's contents. The prompt was clarified to require exactly `input.txt`'s
token, then both complete fresh/resumed checks passed.

## Shared engine

`tests/smoke-engine.py` passed with two Claude Haiku slots and Haiku synthesis, and
with two Codex Luna slots and Luna synthesis, all at low effort. The independent
stages ran from separate disposable checkouts with their supplied context. Both
runs returned a valid passing assessment after checking the tiny candidate; live
candidate and Git-state guards passed. In particular, Codex executed shell commands
inside its private checkout without the earlier `/proc/self/exe` failure.

`tests/evaluate-contributions.sh` also passed all six live Claude matching cases:
`paraphrased_duplicate`, `contextual_addition`, `resolved_revisit`,
`same_line_different_bug`, `covered_in_later_reply`, and
`concluded_without_resolution_flag`.

Reviewer independence now means separate inputs, sessions and workspaces. The
outer container is the security boundary. Peer reports are not deliberately
supplied to a reviewer, but are not kernel-hidden from it.

## Deterministic and packaging checks

`make -j3 check-all EMACS=/tmp/auto-review-test-emacs-wrapper` passed: 747 Python
tests, 189 Bats tests, 162 Emacs ERT tests, shell checks and Python compilation.
The temporary Emacs launcher uses `--no-site-file` and the installed dependency
load paths to avoid the known site-start preload contamination tracked in #31.
It does not change the repository test runner or installed application.

The updated golden fixture intentionally changes Claude argv, reviewer policy
fingerprints and independent-review instructions. A regression test verifies that
bubblewrap-era evidence is invalidated and its old policy metadata is archived,
without relabeling it as a new execution.

Guix packaging validation could not run: no Guix executable or caller-supplied
channel pin is available. `make check-guix` reports the required channel-pin usage.
The recipe removes bubblewrap from both runtime inputs and the installed PATH.

## PR4 replay and reference adjudication

The original configuration was exercised: Claude Opus and Codex Sol at high effort,
Codex Sol synthesis at high effort, base `f7f0f2e`, head `f5e0321`, discussion
withheld. Both independent reviews and synthesis completed. Codex performed real
shell/file checks from its private reviewer checkout; neither provider reported the
former empty-procfs startup failures.

The replay began from the working tree during implementation. Later edits hardened
post-review Git verification and adjusted diagnostics/tests without changing its
model prompts, provider argv or execution-policy identity. It is a single live
sample, not a repeated controlled comparison isolating the cause of quality changes.

Final synthesis contains 14 findings: 10 confirmed, 3 modified/narrowed and 1 new.
Against case C's reference set it retained **6/8 supported behavioral findings and
0/2 observations**. This is an observed reference-recall regression, not a claim
that removing bubblewrap caused it. The reference corpus is not exhaustive, and
newly reported defects do not cancel out missed supported reference cases.

| Reference | Replay finding | Adjudication |
| --- | --- | --- |
| R2: uncertain reply rewritten during reassociation | F11 | Matched, including original receipt/provenance mismatch |
| R5: next reply attempted after PR closes | F14 | Matched by synthesis as a new nonblocking finding, reproduced without a live GitHub write |
| R6: unrelated preflight failure demotes ready verification | F6 | Matched in the supported unchanged-input case |
| R7: own reply invalidates remaining publication/batch | F5 | Matched in the same-thread expected-receipt case |
| R8: unrelated skip bypasses interrupted-task adoption | — | Missed; F2 is a different failed-task skip scenario and is not counted as a match |
| R9: adoption bypasses a queued task's initial fixer | F3 | Matched with the correct narrowed claim; mandatory reviews still remain |
| R10: unrelated Git config causes integrity recovery | — | Missed design-tradeoff observation |
| R11: fork/multiple-remote identity breaks remembered routing | F10 | Matched; backend reproduced, frontend path traced |
| R12: final-review context repeats the accumulated diff | — | Missed efficiency observation; no measured bill or context-limit failure claimed |
| R13: network Owner operations lack cancellation ID | — | Missed; backend operation envelope and frontend cancellation requirement remain in the frozen source |

R8 and R13 remain supported by the historical evidence and frozen source; this run
provides no evidence invalidating them. Their loss is unexplained and is recorded
as a regression rather than dismissed as model variance. R10 and R12 remain
supported observations, separate from the behavioral-defect tally. Full per-reference
reasoning is in [adjudication.json](results/pr4-outer-container/adjudication.json).
The retained [Claude report](results/pr4-outer-container/reviewer-1.md),
[Codex report](results/pr4-outer-container/reviewer-2.md) and
[synthesis](results/pr4-outer-container/synthesis.json) allow this mapping to be checked.
These are assessments of the frozen PR4 source, not findings against the #30 patch.
