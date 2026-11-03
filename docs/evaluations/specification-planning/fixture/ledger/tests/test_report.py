import unittest

from ledger import report
from ledger.models import Entry

ENTRIES = [Entry("2026-01-05", 1250, "food"), Entry("2026-01-20", 500, "food"), Entry("2026-02-01", 3000, "rent")]


class ReportTest(unittest.TestCase):
    def test_format_cents(self):
        self.assertEqual(["12.50", "-0.05", "0.00"], [report.format_cents(c) for c in (1250, -5, 0)])

    def test_totals_by_category(self):
        self.assertEqual({"food": 1750, "rent": 3000}, report.totals_by_category(ENTRIES))

    def test_monthly_totals(self):
        self.assertEqual({"2026-01": 1750, "2026-02": 3000}, report.monthly_totals(ENTRIES))
