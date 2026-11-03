## Outcome
`ledger add DATE AMOUNT CATEGORY [NOTE] --repeat monthly --until YYYY-MM-DD` creates a recurring series. Listing and both reports include every occurrence from the start date through the end date inclusive, with the original amount, category, and note.

Occurrences retain the original day of the month, clamped to each month's last day. For example, a series starting on 2026-01-31 and ending on 2026-03-31 produces January 31, February 28, and March 31.

## Scope
Include recurring additions, persistence, listing, category reports, monthly reports, documentation, and relevant tests. Support only monthly recurrence. Editing, deleting, and other recurrence frequencies are excluded.

## Approach
Build on the existing argparse CLI, `Entry` model, JSON storage, and report functions. Preserve the existing command syntax and output conventions for ordinary entries. Persist series definitions and make their occurrences available consistently to listing and reports. The internal representation, storage extension, and expansion boundary are implementation choices, subject to the compatibility requirements below.

## Interfaces and defaults
Recurrence is opt-in through `--repeat monthly`, which requires `--until` containing a valid calendar date in YYYY-MM-DD form. Ordinary additions without recurrence options continue creating one entry. The existing `--data`, `LEDGER_DATA`, and default data-path behavior remains unchanged.

## Errors and compatibility
An unsupported repeat value, missing or invalid end date for a recurring addition, or end date before the start date returns status 2 and a diagnostic on stderr. These failures leave an existing data file byte-for-byte unchanged and do not create a missing data file.

Existing JSON-list data files remain readable and extendable. Preserve their ordinary entries, including negative amounts and omitted notes. Saving after loading a recurring series, including when appending an ordinary entry, must preserve the series definition without materializing its occurrences into stored rows.

## Assumptions and resolved choices
- Each series includes its start date exactly once. An end date equal to the start date yields one occurrence.
- Every month derives its occurrence from the original start day; February clamping does not shift subsequent months to the 28th or 29th.
- The end date is an inclusive cutoff on occurrence dates. A month contributes no occurrence if its calculated date falls after that cutoff.
- Listing and reports include the complete bounded series, including future occurrences, without a current-date cutoff.
- Generated entries use the existing listing format and chronological ordering. Both reports preserve their existing grouping, sorted keys, and amount formatting.
- Recurrence is optional, and older data remains usable for ordinary and recurring additions.
- The specified recurrence validation failures are usage errors and must not modify persistence.

## Acceptance scenarios
- A January 31 series through March 31 produces January 31, February 28, and March 31 in a non-leap year; the corresponding leap-year case produces February 29.
- A January 31 series ending March 30 includes January and February only. An end date equal to the start includes exactly one entry.
- A series crosses December into January correctly, and future occurrences appear in listing and reports.
- Mixed ordinary entries and multiple series appear once per occurrence in chronological listing; category and monthly reports total those same occurrences. Amounts, categories, notes, and refunds are preserved.
- Persisted data records each series compactly. Reloading, listing, reporting, and later additions neither duplicate occurrences nor replace the series with generated rows.
- A legacy JSON list, including an entry without a note, loads and accepts further ordinary and recurring additions without losing its entries.
- Unsupported repeat values, missing or malformed end dates, impossible calendar dates, and end dates before the start all produce status 2 and stderr diagnostics while preserving existing file bytes or leaving an absent file absent.
- Existing ordinary add, list, report, storage round-trip, and missing-file behavior continues to pass.

## Verification
Extend the CLI, storage, and report tests with the acceptance scenarios, checking actual persisted JSON for compact series storage and file contents before and after rejected additions. Exercise usage errors through the command-line entry point as appropriate to verify exit status and stderr. Run `python -B -m unittest discover -s tests`. The inspected baseline passes all 7 tests.

Acceptance criteria:
- Monthly occurrences use the original day with month-end clamping and an inclusive end-date cutoff.
- Listing and both reports include every generated occurrence with the original amount, category, and note.
- Series persist compactly and survive reloads and subsequent additions without duplication or flattening.
- Legacy data files remain readable and extendable, and ordinary entry behavior remains compatible.
- Specified invalid recurrence inputs return status 2 with stderr diagnostics and leave the data file untouched.
- The existing test suite and added recurrence, persistence, compatibility, and error scenarios pass.