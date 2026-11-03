1. Update `ledger/report.py::totals_by_category` to aggregate using `entry.category.lower()` and retain sorted output. Apply normalization only during reporting.
2. Extend `tests/test_report.py` to cover mixed-case categories, lowercase labels, combined totals including refunds, sorted output, empty input, and unchanged entry categories.
3. Extend CLI/storage tests to verify default and explicit category reports, exact category preservation when adding and listing entries, compatibility with existing mixed-case JSON files, and unchanged file contents after reporting. Verify monthly reports retain their current behavior.
4. Run `python -B -m unittest discover -s tests` after implementation.

Acceptance criteria:
- `Food`, `food`, and `FOOD` produce one `food` total in both `ledger report` and `ledger report --by category`.
- Category report labels are lowercase and sorted; amounts are summed correctly.
- Stored entries and `ledger list` preserve categories exactly as entered.
- Existing JSON files work without migration, and reporting does not modify them.
- Monthly reporting and the full test suite continue to pass.