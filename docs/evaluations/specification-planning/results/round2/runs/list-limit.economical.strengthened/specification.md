## Outcome
`ledger list --limit N` displays the N most recent entries by date, printed oldest first. For dates January 3, 5, and 8, `--limit 2` displays January 5 followed by January 8. Without `--limit`, output is unchanged.

## Scope
Include the list command’s option, validation, help, documentation, and regression coverage. Exclude changes to storage, entry formatting, add, and report behavior.

## Approach
Build on the argparse subcommand parser and existing stable ascending date ordering in `ledger/cli.py`. Preserve the current row formatting and storage order among entries with equal dates. Keep limit validation within CLI argument handling so invalid arguments fail before loading data. Follow the existing unittest patterns; implementation structure and error-stream capture are delegated to the harness.

## Interfaces and defaults
The list subcommand accepts `--limit N`. Omission means no limit. A limit exceeding the entry count displays every entry; an empty or missing ledger displays nothing and succeeds.

## Errors and compatibility
Zero, negative numbers, non-integers, and a missing option value are usage errors: exit status 2, a diagnostic on stderr, and no stdout. Preserve existing data-file selection and output formatting.

## Assumptions and resolved choices
- The issue establishes date as the selection key, ascending display order, positive-integer validation, and unchanged unlimited behavior.
- Limiting means retaining the trailing N rows of the existing stable chronological listing. Equal-date rows retain their existing storage order; when the cutoff splits equal-date rows, later rows in that order are retained.
- The limit counts entries, including entries sharing a date, rather than distinct dates.

## Acceptance scenarios
- Entries stored out of date order produce the latest N entries in ascending date order.
- A limit of 1 displays one latest entry; a limit equal to or larger than the entry count displays all entries.
- Equal-date entries preserve their relative order, including a cutoff within that date.
- Empty and missing ledgers succeed with empty output for a valid limit.
- Invalid values such as `0`, `-1`, `abc`, and `1.5`, and an omitted value after `--limit`, satisfy the usage-error contract.
- Unlimited listing retains its exact formatting and ordering; add and both report modes continue working.

## Verification
Extend CLI coverage in `tests/test_cli.py` for selection, boundary cases, ties, and usage errors, capturing the actual stderr stream used by argparse. Run `python -m unittest discover -s tests`. Check list help and the documented invocation against the implemented interface.

Acceptance criteria:
- --limit N displays the N most recent entries by date, oldest first.
- Equal-date entries preserve existing order, with later rows retained at the cutoff.
- Invalid or missing limit values exit 2, write a diagnostic to stderr, and produce no stdout.
- Valid limits handle empty ledgers and limits exceeding the entry count successfully.
- Unlimited listing, row formatting, storage, add, and report behavior remain unchanged.