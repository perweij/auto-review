# CLI cookbook

Run review and issue commands from a checkout of the target repository. Replace
PR `123` and issue `42` with your numbers. Use `bin/pr-review` and `bin/fix-issue`
for this checkout's version. Authenticate `gh` and the providers used by your
active roles. Model names and efforts must be supported by your installed CLI
and account.

## Generate a complete configuration

Generation needs no target, checkout, authentication, network, or provider tools:

```sh
pr-review --generate-json-config /tmp/pr-settings.json
fix-issue --generate-json-config /tmp/issue-settings.json
```

These commands write built-in defaults from the execution defaults source, never
export a saved session, and refuse to overwrite an existing destination. Edit the
file, then use it:

```sh
pr-review 123 --settings-file /tmp/pr-settings.json --no-publish
fix-issue -i 42 --settings-file /tmp/issue-settings.json
```

Files must be complete. Every referenced name needs a complete
`{"provider", "model", "effort"}` triplet in `model_configs`. PR files require
`reviewers`, `processing`, `synthesis_tests`, and `suggested_changes`. Issue files
require `reviewers`, `processing`, `planning`, `fix`, `synthesis_tests`,
`max_runtime`, `min_weekly_quota_remaining`, and `max_weekly_quota_use`; they exclude
`suggested_changes`. Disabled limits are JSON `null`. Missing fields, unknown
fields, undefined references, duplicate reviewers, and name/provider mismatches
are errors. Accompanying flags cannot repair an incomplete file.

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
  --codex-1-model gpt-6.1-sol --codex-1-effort high \
  --codex-2-model gpt-6.1-terra --codex-2-effort medium \
  --claude-1-model opus --claude-1-effort high
```

Changing `codex-1` changes planning, processing, and the first reviewer. Changing
`codex-2` changes implementation only. Planning assesses the issue and handles
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
  "processing": "codex-1",
  "fix": "codex-2",
  "synthesis_tests": "focused",
  "max_runtime": null,
  "min_weekly_quota_remaining": null,
  "max_weekly_quota_use": null
}
```

Every explicitly defined configuration must be used by a final role. A definition
used only by planning, processing, or fix is valid. If a CLI role override makes a
file definition unused, remove that definition from the file. Inactive built-in
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
  --planning codex-3 --fix codex-4 \
  --codex-4-model gpt-6.1-terra --codex-4-effort medium
```

Fully Claude-only issue work requires every role to select Claude:

```sh
fix-issue -i 42 --reviewers claude-1 --processing claude-1 \
  --planning claude-2 --fix claude-3 --max-runtime 2h --json
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
| Issue implementation | `codex-4` | Codex / GPT-6.1 Sol / high |

Default references are separate. `--economical` selects Haiku and Terra reviewers
with Terra processing; `--expensive` selects Fable and Astra with Astra processing.
Both use high effort and leave issue planning and implementation defaults unchanged.
New reviewer or processing names also use the selected preset's provider defaults.
Names used only for planning or implementation start from built-in provider defaults.
An explicitly shared name keeps one choice for every referring role; saved and
file-defined choices take precedence over presets.

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

Started implementation batches freeze effective model and review settings.
Omitted overrides and equivalent choices resume the same batch, including an
equivalent file with renamed configurations. Renaming configurations or changing
runtime/quota limits preserves the approved plan and cumulative accounting.
Forbidden changes fail with
`settings_changed`, preserving saved settings and evidence.

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
