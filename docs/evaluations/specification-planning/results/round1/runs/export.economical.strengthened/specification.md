## Outcome
`ledger export --format csv|json` exports individual ledger entries for other tools, sorted by ascending date. Amounts are two-decimal strings matching `ledger list`, including negative refunds such as `-3.00`.

## Scope
Include the export command, file-output options, usage documentation, and relevant tests. Preserve existing add, list, report, storage, and data-source behavior. Import, filtering, totals exports, and additional formats are excluded.

## Approach
Extend the existing argparse CLI and continue loading entries through `store.load`. Follow the stable date sorting used by `list`, and reuse `report.format_cents` for amounts. The stored `amount_cents` representation remains unchanged; exports expose `amount` instead.

The existing `cli.main(argv, out, err)` boundary must remain usable by callers and tests. Export output and diagnostics must respect its supplied streams. Parser error handling needs to accommodate the export-specific one-line diagnostic requirement; the current argparse behavior emits usage text and bypasses the supplied error stream. Internal serialization, parser integration, and file-writing design are delegated to the implementation harness.

## Interfaces and defaults
`--format` is required and accepts only `csv` or `json`. Export uses the existing global `--data` option, whose precedence remains explicit path, then `LEDGER_DATA`, then `ledger.json`.

Without `--output FILE`, write the export to standard output. With it, write the export to FILE and print nothing to standard output. An existing destination requires `--force` to overwrite it. Successful exports return status 0.

CSV has the header `date,amount,category,note`, followed by one record per entry, with quoting that preserves commas, quotes, and embedded newlines. JSON is an object with `version` equal to 1 and an `entries` array. Every entry contains exactly `date`, `amount`, `category`, and `note`; all four values are strings. For example: `{"version":1,"entries":[{"date":"2026-01-05","amount":"-3.00","category":"food","note":"refund"}]}`.

## Errors and compatibility
Missing or unknown formats and unwritable destinations return status 2 with a single-line message on stderr and no traceback. An existing destination without `--force` returns status 2 and preserves the file contents.

A missing data file continues to represent an empty ledger. Empty CSV exports contain the header; empty JSON exports contain `{"version":1,"entries":[]}`. Older stored entries without a note continue to load and export with an empty note.

## Assumptions and resolved choices
The maintainer’s comment settles the required format selection, entry-only content, field names, textual amounts, CSV header, versioned JSON shape, destination defaults, overwrite control, and specified errors. Existing repository behavior settles data-source precedence, empty-ledger handling, and missing-note compatibility. Export follows `list` ordering, including preserving source order among entries sharing a date.

## Acceptance scenarios
- Entries stored out of date order export chronologically in both formats; equal-date entries retain their source order.
- Positive, zero, and refund amounts match `ledger list` as two-decimal strings, without its display padding.
- Empty notes and text containing commas, quotes, embedded newlines, and Unicode survive parsing the exported CSV or JSON.
- Empty and missing ledgers produce the defined empty exports; stored entries lacking a note export successfully.
- Standard-output exports and file exports carry equivalent data. File exports emit no standard output, refuse existing destinations without force, and replace them when forced.
- Missing and unknown formats, an existing destination without force, and unwritable paths return status 2. The specified format and path errors produce one stderr line without a traceback.
- Existing data-source selection and add/list/report behavior remain intact.

## Verification
Extend the unittest coverage around the CLI and export behavior. Parse generated CSV and JSON to verify their shapes and text preservation, and exercise destination protection with temporary files. Verify return codes and supplied streams through `cli.main`, and verify actual process exit codes and stderr through `python -m ledger` for format and output-path errors. Run `python -B -m unittest discover -s tests`; the inspected baseline passes all 7 tests.

Acceptance criteria:
- Required --format csv|json exports date-sorted entries with date, amount, category, and note, using two-decimal amount strings.
- CSV has the specified header and preserves quoted text; JSON uses version 1 and the specified entries shape.
- Output defaults to stdout; --output writes only to the file and requires --force to overwrite an existing destination.
- Specified format and unwritable-path errors return 2 with one stderr line and no traceback; overwrite refusal preserves the destination.
- Empty ledgers, missing notes, existing data-source precedence, and add/list/report behavior remain supported.
- Relevant export tests and the complete existing unittest suite pass.