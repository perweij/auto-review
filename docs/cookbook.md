# CLI cookbook

Run these commands from a checkout of the target repository. Replace PR `123`
and issue `42` with your numbers. Examples use the installed `pr-review` command;
use `bin/pr-review` and `bin/fix-issue` to run this checkout's version.
Authenticate `gh` and each provider selected for any role. Model names and effort
levels must be supported by your installed provider CLI and account.

## Two reviewers from the same provider

Two independent Claude reviews, using the same model at different efforts, with
Claude handling synthesis and discussion matching:

```sh
pr-review 123 --no-publish \
  --reviewers claude,claude \
  --claude-model opus \
  --reviewer-1-effort medium \
  --reviewer-2-effort high \
  --processing-provider claude \
  --processing-model opus --processing-effort high
```

Two independent Codex reviews, with different models and efforts:

```sh
pr-review 123 --no-publish \
  --reviewers codex,codex \
  --reviewer-1-model gpt-6.1-terra --reviewer-1-effort medium \
  --reviewer-2-model gpt-6.1-sol --reviewer-2-effort high \
  --processing-provider codex \
  --processing-model gpt-6.1-sol --processing-effort high
```

Slots are ordered: the first entry in `--reviewers` is `reviewer-1`, the second
is `reviewer-2`. They run independently even when their provider and model match.
Select both slots with `--reviewers`; a `--reviewer-2-model` flag alone does not
add a second slot to a saved single-reviewer configuration.

For a same-provider pair, explicitly select the processing provider too. A new
review otherwise defaults to Codex processing, even with `claude,claude`.

## Mixed providers and independent processing

Claude first, Codex second, with a separate Codex processing choice:

```sh
pr-review 123 --no-publish \
  --reviewers claude,codex \
  --reviewer-1-model opus --reviewer-1-effort medium \
  --reviewer-2-model gpt-6.1-sol --reviewer-2-effort high \
  --processing-provider codex \
  --processing-model gpt-6.1-terra --processing-effort high
```

Processing need not use a reviewer provider. For example, run two Codex reviews
and use Claude for synthesis and discussion matching:

```sh
pr-review 123 --no-publish \
  --reviewers codex,codex \
  --codex-model gpt-6.1-sol --codex-effort high \
  --processing-provider claude \
  --processing-model opus --processing-effort high
```

This last combination requires both subscriptions. To use only one provider,
select it for every reviewer slot and for processing.

## One reviewer

Explicit Claude-only operation:

```sh
pr-review 123 --no-publish \
  --reviewers claude --claude-model opus --claude-effort high \
  --processing-provider claude \
  --processing-model opus --processing-effort high
```

Explicit Codex-only operation:

```sh
pr-review 123 --no-publish \
  --reviewers codex --codex-model gpt-6.1-sol --codex-effort high \
  --processing-provider codex \
  --processing-model gpt-6.1-sol --processing-effort high
```

For a fresh review, selecting one reviewer also selects that provider for
processing unless processing was explicitly configured. Saved processing choices
are preserved on resume, so the explicit forms above also work for existing runs.
If the sole reviewer and processing choices match, the pipeline uses a direct
structured review; otherwise it supplies the independent report to processing.

## Which flags affect which roles?

| Flags | Scope |
| --- | --- |
| `--reviewers claude,codex` | Number, order, and providers of the one or two reviewer slots |
| `--claude-model`, `--claude-effort` | All Claude reviewer slots and Claude processing |
| `--codex-model`, `--codex-effort` | All Codex reviewer slots and Codex processing |
| `--reviewer-1-provider/model/effort` | First reviewer only; these are three separate flags |
| `--reviewer-2-provider/model/effort` | Second reviewer only; these are three separate flags |
| `--processing-provider/model/effort` | Processing only; these are three separate flags |
| `--synthesis-tests focused` or `full` | Testing policy during synthesis |

Role-specific model and effort flags override provider-wide flags, regardless of
argument order. For example, this sets both Codex reviewers to Sol/high, then
specializes the first reviewer and processing:

```sh
pr-review 123 --no-publish \
  --reviewers codex,codex \
  --codex-model gpt-6.1-sol --codex-effort high \
  --reviewer-1-model gpt-6.1-terra --reviewer-1-effort medium \
  --processing-provider codex --processing-effort medium \
  --synthesis-tests full
```

## Presets and saved settings

| New-review preset | Claude reviewer | Codex reviewer | Processing |
| --- | --- | --- | --- |
| Default | Opus/high | GPT-6.1 Sol/high | Codex, Sol/high |
| `--economical` | Haiku/high | GPT-6.1 Terra/high | Codex, Terra/high |
| `--expensive` | Fable/high | GPT-6 Astra/high | Codex, Astra/high |

Override parts of a preset with explicit flags:

```sh
pr-review 123 --no-publish --economical --reviewer-2-effort medium
```

Precedence is backend/user defaults, preset, saved settings, then explicit flags.
Presets do not reset an existing review. To change a saved review, supply the
individual flags, optionally selecting its exact saved run:

```sh
pr-review 123 --no-publish --resume /absolute/path/to/run \
  --processing-provider claude \
  --processing-model opus --processing-effort high
```

Compatible completed independent reports can be reused when only processing
changes. With no explicit changes, a resumed run keeps its saved choices.

## Publication and machine-readable output

The examples above save drafts without opening the publication menu. Omit
`--no-publish` for interactive inspection and publication. To authorize automatic
COMMENT publication, replace it with `--auto-publish`:

```sh
pr-review 123 --auto-publish --json \
  --reviewers claude,claude --claude-model opus --claude-effort high \
  --processing-provider claude \
  --processing-model opus --processing-effort high \
  >result.json 2>progress.log
```

For `pr-review`, `--json` requires `--auto-publish`; progress goes to stderr and
the result goes to stdout. Add `--suggested-changes` to generate independently
verified GitHub code suggestions. Automatic publication can stop for findings
requiring attention; see [automatic reviews](manual.md#automatic-reviews).

## Issue implementation and Owner settings

`fix-issue` uses processing settings for issue assessment and planning. Approved
implementation and its independent review use the separate Owner `fix` and
`review` choices. Reviewer slots do not add a post-push PR review to this workflow.

For Claude-only issue work, create an Owner settings file:

```sh
cat > /tmp/owner-claude.json <<'JSON'
{
  "fix": {"provider": "claude", "model": "opus", "effort": "high"},
  "review": {"provider": "claude", "model": "opus", "effort": "high"},
  "verification": "focused"
}
JSON

bin/fix-issue -i 42 \
  --reviewers claude --processing-provider claude \
  --processing-model opus --processing-effort high \
  --settings-file /tmp/owner-claude.json --max-runtime 2h --json
```

For Codex-only work with different implementation and review choices:

```sh
cat > /tmp/owner-codex.json <<'JSON'
{
  "fix": {"provider": "codex", "model": "gpt-6.1-terra", "effort": "medium"},
  "review": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"},
  "verification": "focused"
}
JSON

bin/fix-issue -i 42 \
  --reviewers codex --processing-provider codex \
  --processing-model gpt-6.1-sol --processing-effort high \
  --settings-file /tmp/owner-codex.json --max-runtime 2h --json
```

The two Owner roles can also use different providers. Without a settings file,
new Owner sessions derive both roles from processing; existing sessions retain
their saved Owner choices. Started batches freeze their settings. These examples
retain plan approval; `--yolo` skips approval for that invocation, but unanswered
product questions still block. Runtime is cumulative across invocations.

The Owner JSON requires `verification: "focused"` for compatibility with
interactive Owner. This does not constrain the issue harness's whole-specification
implementation and verification.

For interactive Owner fixes against PR feedback, apply the same file separately:

```sh
pr-review owner 123 settings --settings-file /tmp/owner-claude.json
pr-review owner 123 settings
```

The second command displays saved settings. Reviewer CLI flags do not configure
interactive Owner. See the [Owner workflow](manual.md#pr-owner-workflow) and
[issue workflow](manual.md#issue-fixing) for execution details.
