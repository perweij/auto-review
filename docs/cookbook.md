# CLI cookbook

Run review and issue commands from a checkout of the target repository. Replace
PR `123` and issue `42` with your numbers. Use `bin/pr-review` and `bin/fix-issue`
for this checkout's version. Authenticate `gh` (or `glab` for GitLab workflows) and
the providers used by your active roles. Model names and efforts must be supported by your installed CLI
and account.

## Generate a complete configuration

Generation needs no target, checkout, authentication, network, or provider tools:

```sh
pr-review --generate-json-config /tmp/pr-settings.json
fix-issue --generate-json-config /tmp/issue-settings.json
```

These commands write built-in defaults from the execution defaults source. Add
`--economical` or `--expensive` to generate that preset instead:

```sh
fix-issue --economical --generate-json-config /tmp/economical-issue-settings.json
pr-review --expensive --generate-json-config /tmp/expensive-pr-settings.json
```

Presets follow the same rules as execution: `--economical` covers every issue
role, while `--expensive` leaves independent planning, critique and fixing roles
at their built-in defaults. Generation never
exports a saved session and refuses to overwrite an existing destination. Edit the
file, then use it:

```sh
pr-review 123 --settings-file /tmp/pr-settings.json --no-publish
fix-issue -i 42 --settings-file /tmp/issue-settings.json
```

Files must be complete. Every referenced name needs a complete
`{"provider", "model", "effort"}` triplet in `model_configs`. PR files require
`reviewers`, `processing`, `synthesis_tests`, and `suggested_changes`. Issue files
require `reviewers`, `processing`, `planning`, `planning_review`, `planning_synthesis`,
`fix`, `synthesis_tests`, `max_runtime`, `min_weekly_quota_remaining`, and `max_weekly_quota_use`; they exclude
`suggested_changes`. Disabled limits are JSON `null`. Missing fields, unknown
fields, undefined references, duplicate reviewers, and name/provider mismatches
are errors. Accompanying flags cannot repair an incomplete file.
`planning_review: null` disables critique; `planning_synthesis: null` follows the
planner. For compatibility, an older complete issue file omitting **both** fields
selects the single-assessment workflow; omitting just one is an error.

Generation includes every supported terminal JSON setting. It writes built-in
defaults plus the selected preset; other model and role flags are ignored. Targets, forge selection,
publication, approval, recovery actions and `--max-repair-cycles` remain CLI
controls. Provider timeouts, permissions, budgets and probe settings remain
[environment settings](manual.md#configuration); they are not JSON fields.
Interactive Owner settings use a separate format.

## Two reviewers from the same provider

Independent Claude reviews with separate processing:

```sh
pr-review 123 --no-publish \
  --reviewers claude-1,claude-2 \
  --claude-1-model opus --claude-1-effort medium \
  --claude-2-model opus --claude-2-effort high \
  --processing claude-3 --claude-3-model opus --claude-3-effort high
```

Independent Codex reviews with different choices:

```sh
pr-review 123 --no-publish \
  --reviewers codex-1,codex-2 \
  --codex-1-model gpt-6.1-terra --codex-1-effort medium \
  --codex-2-model gpt-6.1-sol --codex-2-effort high \
  --processing codex-3 --codex-3-model gpt-6.1-sol --codex-3-effort high
```

Names identify configurations, not reviewer positions. Reversing `--reviewers`
reverses execution order while preserving each name's choices. Use one or two
distinct names. Any positive number is supported, including `codex-17`.
Selecting two Claude reviewers leaves default Codex processing active unless you
assign processing explicitly.

## Shared choices and independent roles

All roles reference the same namespace. To deliberately share a choice:

```sh
fix-issue -i 42 \
  --reviewers codex-1,claude-1 \
  --planning codex-1 --processing codex-1 --fix codex-2 \
  --planning-review claude-1 --planning-synthesis same \
  --codex-1-model gpt-6.1-sol --codex-1-effort high \
  --codex-2-model gpt-6.1-terra --codex-2-effort medium \
  --claude-1-model opus --claude-1-effort high
```

Changing `codex-1` changes planning, processing, and the first reviewer. Changing
`codex-2` changes implementation only. `claude-1` shares critique and the second
reviewer; planning synthesis follows `codex-1`. Planning assesses the issue and handles
later planning turns. Processing synthesizes acceptance reviews. The implementation
harness uses `fix`; the completed candidate goes through the shared review engine.
There is no separate post-push PR review.

A complete file equivalent to those model flags is:

```json
{
  "model_configs": {
    "codex-1": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
    "codex-2": {"provider": "codex", "model": "gpt-6.1-terra", "effort": "medium"},
    "claude-1": {"provider": "claude", "model": "opus", "effort": "high"}
  },
  "reviewers": ["codex-1", "claude-1"],
  "planning": "codex-1",
  "planning_review": "claude-1",
  "planning_synthesis": null,
  "processing": "codex-1",
  "fix": "codex-2",
  "synthesis_tests": "focused",
  "max_runtime": null,
  "min_weekly_quota_remaining": null,
  "max_weekly_quota_use": null
}
```

Every explicitly defined configuration must be used by a final role. A definition
used only by planning, critique, synthesis, processing, or fix is valid. If a CLI
role override makes a file definition unused, remove that definition from the file. Inactive built-in
defaults are exempt and do not activate providers.

## Single-provider operation

Claude-only PR review, deliberately sharing reviewer and processing choices:

```sh
pr-review 123 --no-publish --reviewers claude-1 --processing claude-1 \
  --claude-1-model opus --claude-1-effort high
```

Codex-only issue work with independent implementation:

```sh
fix-issue -i 42 --reviewers codex-1 --processing codex-1 \
  --planning codex-3 --planning-review codex-1 --planning-synthesis same --fix codex-4 \
  --codex-4-model gpt-6.1-terra --codex-4-effort medium
```

Fully Claude-only issue work requires every role to select Claude:

```sh
fix-issue -i 42 --reviewers claude-1 --processing claude-1 \
  --planning claude-2 --planning-review claude-4 --planning-synthesis same \
  --fix claude-3 --max-runtime 2h --json
```

A fresh single reviewer implicitly selects independent processing defaults for
that provider. Customizing that reviewer does not customize processing. Saved
processing choices, a complete file, and explicit `--processing` take precedence.
If the sole reviewer's effective triplet matches processing, the engine uses its
direct structured-review optimization; otherwise it runs the independent reviewer
before synthesis.

## Defaults, presets, precedence, and resume

| Role | Default reference | Default choice |
| --- | --- | --- |
| First reviewer | `claude-1` | Claude / Opus / high |
| Second reviewer | `codex-1` | Codex / GPT-6.1 Sol / high |
| Processing | `codex-2` | Codex / GPT-6.1 Sol / high |
| Issue planning | `codex-3` | Codex / GPT-6.1 Sol / high |
| Issue plan critique | `claude-2` | Claude / Opus / high |
| Issue plan synthesis | `null` (same as planner) | Follows issue planning |
| Issue implementation | `codex-4` | Codex / GPT-6.1 Sol / high |

Default model configurations are separate; planning synthesis follows planning.
`--economical` selects Haiku/high and Terra/high reviewers with Terra/high processing.
For a fresh terminal `fix-issue` workflow, it also selects Terra/high planning and
implementation and Haiku/high critique; planning synthesis follows planning.
New names used for any issue role receive their provider's economical defaults.
`--expensive` selects Fable/high and Astra/high reviewers with Astra/high processing.
New reviewer or processing names receive its provider defaults, while independent
issue-only names keep ordinary defaults. An explicitly shared name keeps one
choice for every referring role under either preset.
Saved and file-defined choices take precedence. An existing Owner session supplies
fixing, ordered acceptance reviewers, processing and synthesis testing policy even
for a new issue workflow; economical planning and critique defaults still apply.
Nonterminal API `issue-run`, API `settings` and Emacs presets are unchanged; the
broader economical coverage is specific to terminal issue workflows.

`fix-issue -i 42 --economical` covers all default roles on a fresh job without
imported settings. Optionally share configurations to couple later model/effort
edits across roles:

```sh
fix-issue -i 42 --economical --planning codex-1 --fix codex-1 \
  --planning-review claude-1 --planning-synthesis same
```

On resume, saved choices still take precedence over the preset; use explicit
model/effort overrides to change existing configurations. Preset-only resume
retains saved models, approvals and completed evidence. Selecting a previously
undefined name with `--economical` supplies economical defaults for that name
without changing other saved configurations.

Precedence is **defaults → preset → saved settings → complete file → explicit
CLI flags**, regardless of argument order. Omitted flags retain resolved values.
Files replace applicable saved configuration. Explicit false and disabled values
are supported:

```sh
pr-review 123 --no-publish --settings-file /tmp/pr-settings.json \
  --no-suggested-changes --codex-1-effort medium
fix-issue -i 42 --settings-file /tmp/issue-settings.json \
  --max-runtime none --min-weekly-quota-remaining none --max-weekly-quota-use none
```

Runtime accepts positive durations such as `30m` or `2h`. Quota thresholds accept
percentages greater than zero and at most 100. Runtime and observed quota
consumption remain cumulative when limits change or are disabled.

Resume keeps saved names and sharing. Equivalent PR settings retain the current
generation, human comment edits, and staging. PR resume selects the pending/current
generation; changing processing can reuse compatible independent reports:

```sh
pr-review 123 --no-publish --resume /absolute/path/to/run \
  --processing claude-3 --claude-3-model opus
fix-issue -i 42
```

Reusing a validated ready result or completed issue verification does not require
provider discovery. Polling an unchanged approval or answer wait also needs no
provider discovery. Approval or new discussion requiring model work triggers
validation before that work begins.
Legacy workflows keep their processing-derived implementation choice when imported,
with separate names so later role overrides remain independent.

Stopped issue jobs accept model, effort and provider changes through the same
named flags or complete settings file. Omitted choices keep saved values; changes
apply to unfinished and future model calls without replacing the approved plan.
Completed planning and valid review results keep their original attribution.
A provider change starts a fresh implementation session with saved work and
continuation context; same-provider changes continue the existing session.
Runtime and quota consumption remain cumulative. Source, requirements, trust and
evidence checks still apply. Use `--rerun-review` or `--rerun-planning` when you
explicitly want to regenerate eligible completed stages. Interactive Owner's
non-issue task batches retain their frozen-settings rules.

## Output and publication

The effective configuration is printed to stderr before model work, including
authentication probes. It lists ordered reviewer names, all active roles and
triplets, testing policies, relevant permission/timeout/budget/probe settings,
and issue approval mode and cumulative resource settings. Disabled limits are
shown explicitly. Credentials are omitted.

```sh
pr-review 123 --auto-publish --json --reviewers codex-1 --processing codex-1 \
  >result.json 2>progress.log
fix-issue -i 42 --json >issue-result.json 2>issue-progress.log
```

For PR review, `--json` requires `--auto-publish`. Successful JSON stdout contains
one result. Omit `--no-publish` for the interactive publication menu, or use
`--auto-publish` to authorize automatic COMMENT publication. Add
`--suggested-changes` to generate separately verified GitHub suggestions.
Issue commands retain plan approval; `--yolo` skips approval for that invocation,
but unanswered product questions still block.

## Recover missing issue verification evidence

Normally, resume with the same command. If acceptance needs test evidence, the
coordinator can run supported checks explicitly named in the approved plan in a
fresh candidate checkout, then resume synthesis. It does not restart implementation
just to obtain evidence or rerun checks indefinitely.

If a controller-run check stopped because of an environment problem, fix that
problem and explicitly request another bounded verification round:

```sh
fix-issue -i 42 --rerun-review synthesis
```

This also regenerates synthesis. Ordinary resume retains failed, timed-out and
interrupted outcomes. See the [manual](manual.md#autonomous-issue-execution-and-resource-controls)
for supported commands and limits.

## GitLab review and publication

```sh
pr-review --forge gitlab https://gitlab.com/GROUP/PROJECT/-/merge_requests/123 --no-publish
```

Use an authenticated `glab` and the project's local checkout. The GitLab pilot
supports review, resume, COMMENT finding publication and explicit execution-report
publication. Run without `--no-publish` for the interactive draft menu, or use:

```sh
pr-review --forge gitlab https://gitlab.com/GROUP/PROJECT/-/merge_requests/123 --economical --auto-publish --json
```

The automatic workflow leaves uncertain findings and revisits unstaged and publishes eligible peers.
Other publication blockers still stop the batch.
Native inline lines/ranges and overall assessments are supported. Approvals,
request-changes, Reviewer replies, suggestions and `fix-issue` remain unsupported.
Owner supports native conversations and same-project fixing. Saved suggestion settings can be disabled with
`--no-suggested-changes`. See the
[pilot capability and evidence notes](gitlab-pilot.md).

In Emacs, select the MR in Forge and press `V`, or run
`C-u M-x magit-auto-review` and enter its number or URL. From a GitLab checkout,
`M-x magit-auto-review` offers saved reviews and MR entry. Use `R r` to configure
and run, edit with `e`, stage with `s`, and use `P p` to inspect frozen outgoing
notes/positions and confirm. `P r` recovers uncertain receipts. `C` reads saved
conversations, `w` opens the checkout and `J` opens report actions. Unsupported
actions are omitted. GitLab defaults to Reviewer; `M` remembers Owner per project. Remote MR browsing,
and issue fixing remain deferred.

## Compatibility

Provider-wide `--codex-model/effort` and `--claude-model/effort`, positional
`--reviewer-N-provider/model/effort`, provider-only reviewer lists, and direct
`--processing-provider/model/effort` (also planning/fix) are rejected with named
configuration guidance.

The API, Emacs slot representation, and interactive Owner settings interface are
unchanged. Historical saved sessions remain readable. Owner-format settings files
remain supported by `pr-review owner ... settings --settings-file`, but are not
the new terminal file format. Interactive Owner's focused task-check `review`
role is outside the issue CLI schema and does not activate a provider for issue
execution. See the [manual](manual.md#issue-fixing) for workflow details.

## Draft, review and synthesize an issue plan

```sh
fix-issue -i 42 --max-runtime 2h
fix-issue -i 42 --planning-review claude-2 --claude-2-effort medium
fix-issue -i 42 --rerun-planning review
```

The first command uses Sol/high for drafting and synthesis and Opus/high for
critique. The second changes future critique calls while preserving completed
work. The third explicitly regenerates critique and synthesis before implementation
has started. Use `--planning-review none` for a single assessment, or
`--planning-synthesis NAME` for an explicit synthesis model. New jobs count
planning and execution against the same runtime allowance; approval waits do not
consume it. Resume preserves the original scope of older jobs.

## Explain a long issue session

Enable an execution report for a new or resumed job:

```sh
fix-issue -i 42 --economical --session-report
```

New issue sessions always record stage outcomes and dependencies locally. This
flag can therefore be enabled later; `--no-session-report` disables automatic
generation/publication without stopping recording. The preference persists. Reports
are saved under the issue session's `reports/` directory. Successful completion
also posts a separate issue comment. An incomplete run retains a local report.
The default view is compact; expand the time/token breakdown, execution history
and stage outcomes for details. No additional model calls are needed.

Generate a local report from a session created with version-2 tracing, without resuming it:

```sh
fix-issue --report-session /absolute/path/to/issues-v1/project/issue-42
```

This command requires no authentication or network access and never publishes.
Older sessions are unsupported for reporting and are never reconstructed. They
remain resumable. Processing totals exclude time between stopped invocations.
Missing observations and unavailable waiting measurements remain unknown.
Reported tokens include repeated context and cache reads according to the usage
ledger; their proportions do not represent monetary cost. Provider-internal
verification is included in the provider call; controller verification is separate.

### Inspect an Owner fixing round

New standalone Owner batches automatically save execution reports, including when
fixing pauses. In the Owner dashboard, select a fixing batch and press `E`, then
`o` to open its local report. `E p` explicitly publishes a snapshot to the PR
discussion. Neither action runs models or publishes feedback replies.

From the terminal:

```sh
bin/pr-review owner 123 report
bin/pr-review owner 123 publish-report
```

Both commands default to the latest batch; add `--batch ID` to select an older
one. Repeating publication of the same snapshot does not send another comment.
Further execution permits a new snapshot. Existing batches without tracing remain
usable but cannot be reconstructed into execution reports.

### Inspect a plain review's execution

New PR/MR review generations automatically save an execution report. The CLI
prints its path, and `J o` in reviewer mode opens the displayed generation's
report. `J p` explicitly publishes a GitHub snapshot, including a paused run.
`J` also opens Owner's report menu; its existing `E` shortcut remains available.
Reviewer `E` still elaborates a finding.

```sh
bin/pr-review report SESSION
bin/pr-review publish-report SESSION --generation GENERATION
```

Commands default to the current generation; use `--generation` for pending or
historical views. Reports do not run models. The same CLI commands publish GitLab
reports as MR timeline notes. Repeating a command recovers its receipt; an
uncertain send without a visible receipt stops rather than risking a duplicate.
`--auto-publish` publishes findings, not execution reports. Historical generations
without traces remain runnable but cannot be reconstructed into reports.

## Address GitLab MR feedback with Owner

```sh
pr-review owner https://gitlab.com/GROUP/PROJECT/-/merge_requests/7 open --forge gitlab
pr-review owner refresh --session SESSION --forge gitlab
pr-review owner reply --session SESSION --item KEY --body-file reply.md
pr-review owner triage --session SESSION --operation stage --reply REPLY_ID
pr-review owner prepare --session SESSION
pr-review owner publish --session SESSION --confirm TOKEN
```

Read `KEY` and `REPLY_ID` from the returned Owner view. Inspect the frozen preview
before using its token. Native standalone replies create threads. `resolve` and
`reopen` take `--item KEY`; permission checks and read-back are explicit.

For an open same-project MR, `fix --item KEY` runs the shared fixing and
verification workflow. `--auto-commit` optionally commits verified changes; push
explicitly, then `associate` and preview/publish the fixed-in replies. Fork MRs
support conversations only. In Emacs select Owner with `M` and use the same Owner
keys as for GitHub. `O` recovers uncertain receipt delivery without resending.
