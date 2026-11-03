import json
import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import Harness

h = Harness(sys.argv[1])


def seed():
    h.fresh()
    for date, amount, category in (("2026-01-04", "4.00", "Food"), ("2026-01-02", "2.50", "food"), ("2026-01-01", "10", "Rent"),
                                   ("2026-02-03", "1", "FOOD")):
        assert h.cli("add", date, amount, category)[0] == 0


def a1():
    seed()
    code, out, _ = h.cli("report")
    return code == 0 and out.splitlines() == ["food: 7.50", "rent: 10.00"]


def a2():
    seed()
    with open(h.data, encoding="utf-8") as stream:
        stored = sorted(item["category"] for item in json.load(stream))
    return stored == ["FOOD", "Food", "Rent", "food"]


def a3():
    seed()
    code, out, _ = h.cli("list")
    return code == 0 and any(line.endswith("Food") for line in out.splitlines()) and any("Rent" in line for line in out.splitlines())


def a4():
    sys.path.insert(0, h.checkout)
    import importlib
    report = importlib.import_module("ledger.report")
    models = importlib.import_module("ledger.models")
    entries = [models.Entry("2026-01-01", 100, "Zed"), models.Entry("2026-01-02", 50, "alpha"), models.Entry("2026-01-03", 25, "ZED")]
    return report.totals_by_category(entries) == {"alpha": 50, "zed": 125} and list(report.totals_by_category(entries)) == ["alpha", "zed"]


def a5():
    seed()
    code, out, _ = h.cli("report", "--by", "month")
    return code == 0 and out.splitlines() == ["2026-01: 16.50", "2026-02: 1.00"]


def r1():
    # A legacy data file (hand written, mixed case) still loads and reports.
    h.fresh()
    with open(h.data, "w", encoding="utf-8") as stream:
        json.dump([{"date": "2025-12-01", "amount_cents": 500, "category": "Misc"}], stream)
    code, out, _ = h.cli("report")
    return code == 0 and out.strip() == "misc: 5.00"


h.acceptance_check("A1_combined_lowercase_totals", a1)
h.acceptance_check("A2_storage_keeps_original_case", a2)
h.acceptance_check("A3_list_shows_original_case", a3)
h.acceptance_check("A4_library_totals_sorted_lowercase", a4)
h.acceptance_check("A5_monthly_unchanged", a5)
h.regression_check("R1_legacy_file_reports", r1)
h.common_regressions()
h.finish()
