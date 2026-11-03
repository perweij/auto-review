1. Extend ledger/cli.py with export, required --format csv|json, optional --output FILE, and --force. Handle missing or unknown formats with exit status 2 and exactly one stderr line; argparse currently emits multiline errors, so adapt export validation accordingly.
2. Load entries through store.load, sort ascending by date using the same stable ordering as list, and serialize date, amount, category, and note. Reuse report.format_cents for amount strings, including negative refunds.
3. Use the standard CSV writer with header date,amount,category,note and proper quoting. Produce JSON as {"version": 1, "entries": [...]}, with amounts represented as strings. Empty ledgers produce a CSV header or an empty JSON entries array.
4. Write to stdout by default. For --output, write UTF-8 to the file with no stdout output. Use exclusive creation unless --force permits replacement. Catch destination open/write failures and return 2 with a single stderr line and no traceback.
5. Add focused export tests alongside tests/test_cli.py, including subprocess checks for actual stdout, stderr, and exit status. Document invocation examples, formats, and overwrite behavior in README.md.
6. Run python -m unittest discover -s tests after implementation, retaining coverage for existing add, list, report, and storage behavior.

Acceptance criteria:
- Both formats export individual entries in ascending date order with exactly the four specified fields and two-decimal amount strings.
- CSV quotes commas, quotes, and newlines correctly; JSON has version 1 and an entries array. Unicode, empty notes, refunds, and empty ledgers round-trip through their respective parsers.
- Default output goes to stdout; --output writes the file and leaves stdout empty.
- An existing destination remains unchanged and returns status 2 without --force; --force replaces its contents successfully.
- Missing --format, unknown formats, and unwritable destinations return status 2 with one stderr line and no traceback.
- The full existing test suite and new export tests pass.