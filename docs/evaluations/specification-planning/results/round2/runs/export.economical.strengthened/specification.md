## Outcome
`ledger export --format csv|json` exports all ledger entries in ascending date order for use in other tools. Amounts are strings matching `ledger list`, including two decimal places and negative refunds.

CSV begins with `date,amount,category,note` and quotes fields as needed. JSON has the shape `{"version": 1, "entries": [{"date": "2026-01-05", "amount": "12.50", "category": "food", "note": "lunch"}]}`.

## Scope
Include the export command, its CLI documentation, and verification of serialization, destinations, and errors. Export entries only. Filtering, totals, importing, and changes to the stored ledger schema are excluded. Existing add, list, and report behavior remains compatible.

## Approach
Build on the command parser and dispatch in `ledger/cli.py`, entry loading in `ledger/store.py`, and amount formatting in `ledger/report.py`. Preserve the existing `--data` selection and `LEDGER_DATA` fallback. Follow `list` ordering and use the loaded entry values, including the loader’s empty-note fallback. Serialization structure and file-writing implementation are left to the harness.

The export error contract must work both through `python -m ledger` and the injectable output streams supported by `cli.main`; the current parser emits usage errors outside those injected streams, so export validation needs appropriate handling.

## Interfaces and defaults
`--format csv|json` is required; there is no default format. Output goes to standard output unless `--output FILE` is supplied. Successful file export writes no standard output. Successful exports return status 0.

`--force` permits replacing an existing output file. Without it, an existing destination is preserved and the command fails. Both formats expose exactly the fields `date`, `amount`, `category`, and `note`; CSV uses that column order. JSON amounts are strings, and the top-level version is the integer 1.

## Errors and compatibility
Missing or unknown formats, an existing output file without `--force`, and destinations that cannot be written return status 2 with one line on standard error and no traceback or standard output. Missing or invalid format selection must not create or replace an output file.

A missing data file continues to represent an empty ledger. Older entries without a note export an empty string. Existing commands, storage representation, and their error behavior remain compatible.

## Assumptions and resolved choices
- The maintainer specifies CSV and JSON with mandatory format selection, entry-level content, four fields, and the versioned JSON envelope.
- Amount text uses the same formatter as `list`, preserving two decimals and refund signs.
- Ascending date order follows `list`; entries sharing a date retain their stored order, following its stable sorting behavior.
- Standard output is the default destination; explicit file output is silent, and replacing an existing file requires `--force`.
- The specified export failures use status 2 and one-line stderr messages. Exact message wording is left to implementation.
- Empty ledgers produce a CSV header with no data rows or `{"version": 1, "entries": []}`. Storage’s existing empty-ledger and missing-note behavior carries through to export.

## Acceptance scenarios
- Entries stored out of date order export chronologically in both formats; equal-date entries retain their relative order.
- Positive, zero, and negative amounts export as two-decimal strings. Empty notes and category or note text containing commas, quotes, newlines, or Unicode round-trip through the chosen format.
- An empty or missing ledger exports valid empty output in either format.
- Default output is readable from stdout. `--output` writes equivalent content to a new file with empty stdout; existing files remain unchanged without `--force` and are replaced when it is supplied.
- Missing format, unsupported format, existing destination without force, and an unwritable destination each produce the specified failure contract.
- Existing add/list/report operations and data-source selection continue to work.

## Verification
Extend the existing unittest coverage with parsed CSV and JSON assertions, ordering and amount cases, empty and older-entry cases, destination handling, and failure contracts. Exercise export through both `cli.main` with captured streams and the module entry point, especially parser errors. Use temporary destinations for file cases. Run `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests`; the inspected baseline passes all 7 existing tests.

Acceptance criteria:
- Export requires --format csv|json and emits all entries in ascending date order with stable ordering for equal dates.
- CSV has the specified header and valid quoting; JSON has version 1 and entries with exactly date, amount, category, and note.
- Amounts are two-decimal strings matching ledger list, including negative refunds.
- Output defaults to stdout; --output writes silently, and replacing an existing file requires --force.
- Specified export failures return status 2 with one stderr line, no traceback, and no stdout.
- Empty ledgers, missing notes, data-source selection, and existing commands remain compatible; the full unittest suite passes.