# Cases established before implementation

Follow-up authorized after the repair-only experiment. This is a sequential
experiment, not an additional repair of that candidate or of the live PR.

## Protocol

- Start with the historical source before configuration implementation, using the
  baseline in `provenance.json`; do not apply either repair or the manual patch.
- Supply the approved issue context and the original configuration step's expected
  outcome. Restrict this evaluation to that one step so it does not run the full
  eleven-step feature implementation.
- Use the existing production planning call and schemas to select up to eight
  cases before coding. Planner guidance now explicitly derives cases from public
  input sources and normalization/loading boundaries rather than reported bugs.
- Run the production `owner_fixes.run_task` loop: implementation, independent
  review, at most one repair and another review. Maximum five model calls including
  planning, all Terra/high. No escalation, counter reset or manual candidate edit.
- Keep external checks outside model context. Score the original 15 checks plus
  four preset/default combinations motivated by the previous review. Added checks
  were fixed while planning/implementation was running, before external scoring;
  they are never sent to a model. Report both scores separately.
- Success requires the independent task review to pass and all 19 external checks
  to pass. This is configuration-step success, not whole-issue readiness.
- Preserve every attempt, including incomplete output. A failure must not trigger
  another unrecorded experiment or a live-PR resume.

This changes both the starting point and case-selection timing relative to the
repair-only run. Its model-call/token totals are not an apples-to-apples efficiency
comparison. One run cannot establish general model reliability.

Run explicitly with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/evaluations/owner-compatibility/planned.py /tmp/new-planned-run --live
```

The output directory must not exist. Repositories have no remote. The source,
checkpoints and artifacts are disposable; live PR files and journals are untouched.

## Live recovery inspection

Read-only inspection before this experiment found the live batch still paused at
`p2-t1`, with one repair consumed and ten subsequent tasks queued. The four files
in the archived manual delta still exactly match the archived reference. No
intervening differences were found in those files. This observation is not an
authorization to overwrite later edits: compare again before any reconciliation.

The CLI currently exposes `--adopt` but not explicit replanning. If this experiment
passes, add/test a journal-preserving CLI replan action before proposing the exact
live recovery command. Adoption alone does not replenish an exhausted repair.

## Result: acceptance failed

The planner selected eight cases, including legacy saved-state precedence and
canonical round trips. Implementation and its independent review completed;
the review rejected inherited processing overriding a legacy reviewer request
and malformed layered slot input raising `KeyError`. The one permitted repair
ran, and a second independent review rejected losses of explicitly saved
processing and distinct saved slot settings after reviewer-only overrides.

| Candidate | Original behavioral checks | Preset checks | Total |
|---|---:|---:|---:|
| Earlier control repair | 8/15 | 1/4 | 9/19 |
| Earlier repair with cases | 12/15 | 1/4 | 13/19 |
| Cases planned before coding | 11/15 | 4/4 | 15/19 |

The new implementation fixes the previously missed legacy saved-state and preset
cases, but loses explicit processing defaults for both providers in both legacy
and canonical representations. Independent review also found saved canonical
settings lost on reviewer-only overrides; these extra findings are not included
in the declared 19-case score. All supplied case groups were reported as tested,
yet the reviewer correctly looked beyond those groups and rejected the task.

The first external run scored 10/15 because one check called `apply_overrides`,
a private helper introduced by the earlier candidate, absent in this new one.
That was an evaluator error, not a behavior defect. It was replaced by a public
`settings.resolve` saved-session/reviewer-override check. Rechecking every prior
candidate left their original 15-case scores unchanged. The new candidate passes
that check, giving 11/15 plus 4/4 preset checks. The running harness had loaded the
original 15-case scorer before the extension; final external scoring is saved
separately from the untouched raw summary. No implementation edits or model
reruns were made to obtain the corrected score.

Five model calls took 222 seconds and used 1,533,863 input tokens, of which
1,338,752 were cached (195,111 uncached), and 17,565 output tokens. There were 53
command tool calls. These include planning and initial implementation, unlike the
earlier repair-only runs; they do not establish an efficiency regression or gain.

## Assessment and recovery decision

Moving cases earlier helped some boundaries but did not establish reliable
one-repair recovery. A larger list of cases is not by itself a root-cause fix.
The persistent coding problem is loss of value provenance: generated code treats
an inherited default and an explicit choice alike, then repairs one precedence
case by changing both. Broad case groups also leave individual combinations
unexamined even when their evidence is marked tested.

Keep the live PR paused. Do not automatically increase retries, rerun the same
experiment until it passes, or describe these results as sufficient prevention.
The next useful investigation is whether the task contract can state a consistent
precedence/provenance rule, with explicit inherited-versus-selected examples,
before implementation. Conflicting expectations should become a clarification
instead of alternating fixes. That investigation should precede further model
spending or a routing change; independent review must remain the final gate.

No CLI replan action or live reconciliation was performed because the experiment
did not pass its prerequisite. The archived manual delta, live files, task history,
and consumed repair allowance remain intact.

The 15 focused Owner contract tests and replay-script compilation passed after
the planner guidance change; `git diff --check` passed. The prior full-suite result
remains recorded in the main evaluation. This follow-up changed planner guidance
and evaluation tooling, not the workflow's execution/budget logic.

Artifacts are in `results/planned/`: plan, all five prompts/schemas/results,
generated patch, task evidence, corrected summary and original raw summary.
Raw logs remain under `/tmp/owner-compatibility-planned-1/`.

## Independent Fable consultation

Fable/medium reviewed a self-contained evidence packet in one tool-free call
(139.5 seconds). Its full response and input are retained in `results/fable/`.
No implementation changes or new repair trials followed the consultation.

Its central criticism is that the approved scope does not define which lower-layer
processing values count as explicit. The first planned review broadly required
reviewer-only overrides to replace provisional or saved processing unless the
request explicitly selected processing. The second required explicit canonical
saved processing to survive such an override. Those rules can coexist only if
the contract distinguishes origins/formats or unchanged versus changed reviewers;
the earlier instruction did not make that distinction. The external checks also
assume that a processing value supplied in `defaults` represents explicit intent.
That is a policy assumption, not an established neutral oracle.

We checked Fable's concrete uncertainty in the frontend: in
`emacs/magit-auto-review-setup.el`, `magit-auto-review-processing-provider` defaults
to `"codex"`, and `magit-auto-review--default-settings` always serializes it.
It does not test whether the user customized it. `magit-auto-review-setup-submit`
also sets processing to the sole selected reviewer before manual launch. Therefore presence of this legacy
defaults field alone cannot establish a user's explicit independent-processing
choice. In contrast, CLI processing flags use `argparse.SUPPRESS`, so an absent
flag remains absent in the request. The earlier claim that origin loss alone
explained the failures was incomplete: some expected origin semantics were never
agreed and were inconsistently described to the fixer.

Fable recommends no additional pipeline machinery or retry allowance. Existing
case fields can express concrete layer/value/request/expected-winner tuples;
topic-sized cases are too broad. It also cautions against making 19/19 on the
current disputed oracle a prerequisite for all future live recovery.

Revised next step: agree on the semantics of legacy frontend defaults, canonical
saved processing under reviewer-only changes, and preservation of unchanged slot
choices. Then derive explicit cases from those decisions and rescore the frozen
candidates without model calls. Keep the historical scores as observations under
the old oracle, not proof of a settled product contract. Only afterward choose a
journaled replan or inspected adoption. The live PR remains paused pending that
decision and independent verification, not merely a particular historical score.

Scope correction: `bin/fix-issue` has no Emacs frontend. The Emacs observation is
a separate compatibility finding for issue #10's changes to `pr-review` and its
shared backend, not an explanation of the CLI dogfood request's inputs. The
[implementation-plan follow-up](../../plans/owner-compatibility-repairs.md#issue-10-follow-up-emacs-processing-defaults-and-independent-selection)
records the evidence, required policy decision and acceptance checks so this work
is not lost. Diagnose the actual `fix-issue` CLI/saved-state inputs independently.

## CLI trace and policy decisions

Read-only inspection of the live issue-10 record found reviewers Claude and
Codex, processing Codex, Claude model Haiku, Codex model Terra, both at high effort.
The Owner session independently records Codex/Terra/high for both fix and review.
This matches the economical preset; the saved state does not retain the complete
original argv, so it cannot prove which flags produced those values.

The code path is `issue_cli.main` to `issues.Workflow.run`: built-in/user defaults
and preset, then saved issue configuration, then supplied individual flags, then
validation. Owner creation receives that resolved configuration as `defaults`;
`owner_state.default_settings` derives fix/review choices from its processing
provider. Existing Owner sessions retain saved choices. There is no Emacs call in
this path. The recorded provider choice is consistent; the single-reviewer
precedence defects are concerns about the feature being generated for issue #10,
not evidence of wrong provider selection by this dogfood invocation.

The user confirmed that repeating an ordered reviewer list preserves the saved
choices, then explicitly said "Preserve the saved choices." The clarified rule
is: saved choices stay unchanged unless explicitly overridden; changing
`--reviewers` alone does not reset saved independent processing. This supersedes
the earlier proposed fallback rule wherever an effective saved choice exists.
It does not declare the always-present Emacs default to be an explicit choice,
nor settle every fresh-session frontend-default case. That remains the separate
Emacs follow-up.

The historical oracle is retained unchanged to preserve the earlier experiments.
`results/fable/policy-observations.json` records raw public-API observations from
the four existing candidates, without pass/fail judgments or model calls. All
four currently replace distinct saved slot models on an unchanged provider-only
request, and all replace independent saved processing when narrowing to one
reviewer. Thus accepting the recommended policies would leave concrete code
defects even in the manual reference; policy clarification alone would not make
that reference ready for adoption. Live files and counters remain untouched.

## Ratified saved-choice contract and zero-call rescore

For these checks, the saved canonical configuration has two Claude slots:
`reviewer-1 / first-model / low` and `reviewer-2 / second-model / high`, with
Codex processing `saved-processor / low`. Current built-in defaults are lower
priority than these saved values. Every case is also checked with the providers
swapped. No catalogs or provider executables are needed for settings resolution.

| Saved input / explicit request | Required behavior |
|---|---|
| Canonical configuration above; `--reviewers claude,claude` | Both slot IDs, models and efforts, and processing, stay unchanged. |
| Same saved configuration; `--reviewers claude` | Reviewer count becomes one; saved Codex processing model and effort remain unchanged. This check does not prescribe a general slot-matching algorithm for changed lists. |
| Legacy mixed Claude/Codex reviewers, effective saved Codex processing `saved-processor / low`; `--reviewers claude` | Retain that effective processing choice. This differs from a legacy sole-reviewer manifest whose other-provider field was historically ignored. |
| Canonical configuration above; same reviewers plus `--processing-provider claude` | Processing provider changes to Claude; the two reviewer slots remain untouched. No expectation is imposed on unspecified processor-model fallback here. |
| Canonical configuration above; same reviewers plus processing object `claude / replacement-processor / high` | Use that exact processing object; the two reviewer slots remain untouched. |
| Canonical configuration above; current defaults and economical preset, with no individual role overrides | Saved reviewer and processing choices win over those provisional settings. |

`check_saved_choices.py` expresses this contract through the public `resolve`
entry point and checks that loading never mutates the supplied manifest. It is
separate from `check_cases.py`: in particular, the old legacy-session test that
expected saved processing to switch with reviewers is historical evidence, not
an acceptance condition for the clarified policy. Fresh default-origin questions
are not silently ratified by this new checker.

Results with no model calls or source edits:

| Frozen candidate | Saved-choice checks |
|---|---:|
| Earlier control repair | 2/12 |
| Earlier repair with cases | 2/12 |
| Earlier manual reference | 2/12 |
| Planning-first candidate | 2/12 |

All pass preservation against provisional presets. All fail the unchanged-slot,
reviewer-narrowing and effective legacy-processing requirements. Explicit
processing override cases fail because they also reset reviewer slots; the
explicit processing-provider selection itself is not necessarily wrong. These
12 targeted cases are a different denominator from the old 19-case suite and
must not be presented as a model-quality regression.

Results are retained in `results/saved-choices/`. This separates the hypotheses:
the specification was underspecified, but none of the existing candidates becomes
correct merely by clarifying it. Remaining work is a configuration overlay/migration
fix under this contract, followed by independent verification. Do not add another
general case schema, automatically reset the exhausted batch, or adopt the manual
reference as if it already satisfies the agreed semantics.

The next recovery step is a journaled replan carrying these decisions into the
task context. Before that live action, reconcile the archived manual delta and
provide a tested CLI recovery route. Neither a local documentation edit nor this
rescore updates the approved issue discussion or the live Owner task contract.

## Explicit CLI convenience

The user subsequently requested the convenience flag. `fix-issue -i 10 --replan`
(or `-p 11`) now delegates to Owner's existing replan operation for one unfinished
batch. It preserves completed work and archives superseded task repair counts;
it neither adopts the earlier manual edits nor overrides approval, trust, frozen
settings, or commit recovery. It does not queue a replan across an approval wait.
No live invocation was performed. See the manual for usage and the focused
`tests/test_issue_replan.py` coverage for journal retention and recovery guards.

## Live retry preparation

At the user's request to prepare everything before their command, the archived
manual delta was removed from exactly four files after checking the entire
worktree tree against the archived reference. The restored tree is
`1a708d7e9153219b890cfc4acaff6c6adc4fdee4`, exactly the failed agent checkpoint.
HEAD, the real index, issue/Owner session journals and repair counters were
verified unchanged. Existing agent changes, including the untracked test file,
remain in the worktree. No `fix-issue` invocation or model call was made.

The reversible backup is
`/home/vscode/.local/state/pr-review/manual-recovery/issue-10-xtebbz6_`;
`results/issue-10-reconciliation.json` records the check and restored files.
The approved clarification is drafted in `issue-10-clarification.txt` for posting
to issue #10 before the retry. The connected GitHub integration could read the
issue but rejected comment creation with HTTP 403; authenticated `gh` is the
fallback. A revised issue plan may require approval on the next invocation.

Validation before committing: the full Python suite had passed 532 tests; lint
and 38 focused issue-step, replan, and contract tests passed again during retry
preparation. The command should run from the main checkout using its updated
`bin/fix-issue`, rather than the older script inside the PR worktree.
