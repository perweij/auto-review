## Outcome
Category reports combine categories using their lowercase labels. Entries for `Food` (12.50), `food` (5.00), and `FOOD` (-2.50) produce one line: `food: 15.00`.

## Scope
Apply this behavior to `ledger report` and `ledger report --by category`, including the shared category aggregation function. Preserve stored categories, list output, monthly reports, and the existing JSON format. No data migration or new command-line options are required.

## Approach
Build on `ledger/report.py` and its existing sorted category aggregation. `ledger/cli.py` already routes both default and explicit category reports through that function. Keep normalization confined to report grouping and labels; retain the existing entry model and storage behavior. Internal implementation and test structure are delegated to the coding harness.

## Assumptions and resolved choices
- Lowercase category labels define the groups: categories with the same lowercase value share one total, displayed using that value.
- Preserve the existing alphabetical ordering, integer-cent arithmetic, and amount formatting.
- Categories remain exactly as entered in saved entries and `ledger list`; reporting must not mutate entries or rewrite data files.
- Existing JSON files work directly, including files containing mixed-case categories. Monthly report behavior remains unchanged.

## Acceptance scenarios
- The mixed-case entries in the outcome example produce exactly `food: 15.00` in both default and explicit category reports.
- A category entered only as `Rent` appears as `rent`; distinct lowercase labels remain separate and sorted.
- Adding mixed-case categories preserves their exact spelling in JSON and subsequent list output. Reporting leaves the file contents unchanged.
- A preexisting JSON file containing case variants reports a combined lowercase total without migration and still lists the original spellings.
- Existing lowercase-only totals, empty reports, refunds, monthly totals, and list ordering continue to behave as before.

## Verification
Extend the existing unittest coverage in `tests/test_report.py`, `tests/test_cli.py`, and, where useful, `tests/test_store.py` to demonstrate grouping, lowercase display, preservation, and compatibility. Run `python -m unittest discover -s tests`. During planning, a read-only in-memory reproduction confirmed that the current implementation reports `FOOD`, `Food`, and `food` separately; the test suite was inspected but not executed.

Acceptance criteria:
- Default and explicit category reports combine case variants into one lowercase category total.
- Category report ordering, cent arithmetic, and amount formatting remain unchanged.
- Stored entries and list output preserve categories exactly as entered; reporting does not modify the data file.
- Existing JSON files work without migration, and monthly reports retain existing behavior.
- Regression coverage passes with `python -m unittest discover -s tests`.