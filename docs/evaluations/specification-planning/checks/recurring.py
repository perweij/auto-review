import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from common import Harness

h = Harness(sys.argv[1])


def dates(out):
    return [line.split()[0] for line in out.splitlines()]


def a1():
    h.fresh()
    code = h.cli("add", "2026-01-31", "10", "gym", "--repeat", "monthly", "--until", "2026-04-30")[0]
    out = h.cli("list")[1]
    return code == 0 and dates(out) == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"] and all("10.00" in l for l in out.splitlines())


def a2():
    h.fresh()
    code = h.cli("add", "2028-01-30", "5", "gym", "--repeat", "monthly", "--until", "2028-03-30")[0]
    return code == 0 and dates(h.cli("list")[1]) == ["2028-01-30", "2028-02-29", "2028-03-30"]


def a3():
    h.fresh()
    h.cli("add", "2026-01-15", "10", "gym", "--repeat", "monthly", "--until", "2026-03-15")
    h.cli("add", "2026-02-01", "3", "food")
    code, out, _ = h.cli("report", "--by", "month")
    return code == 0 and out.splitlines() == ["2026-01: 10.00", "2026-02: 13.00", "2026-03: 10.00"]


def a4():
    h.fresh()
    code, out, err = h.cli("add", "2026-05-01", "10", "gym", "--repeat", "monthly", "--until", "2026-04-30")
    return code == 2 and err.strip() != "" and not os.path.exists(h.data)


def a5():
    h.fresh()
    results = []
    for argv in (["--repeat", "weekly", "--until", "2026-05-01"], ["--repeat", "monthly", "--until", "not-a-date"],
                 ["--repeat", "monthly", "--until", "2026-13-01"]):
        code, _, err = h.cli("add", "2026-01-01", "10", "gym", *argv)
        results.append(code == 2 and err.strip() != "")
    return all(results) and not os.path.exists(h.data)


def a6():
    h.fresh()
    code = h.cli("add", "2026-01-01", "10", "gym", "--repeat", "monthly", "--until", "2031-01-01")[0]
    size = os.path.getsize(h.data)
    count = len(dates(h.cli("list")[1]))
    return code == 0 and count == 61 and size < 1500


def a7():
    h.fresh()
    with open(h.data, "w", encoding="utf-8") as stream:
        json.dump([{"date": "2025-12-01", "amount_cents": 500, "category": "misc", "note": "old"}], stream)
    ok_list = dates(h.cli("list")[1]) == ["2025-12-01"]
    ok_add = h.cli("add", "2026-01-01", "7", "misc")[0] == 0
    return ok_list and ok_add and dates(h.cli("list")[1]) == ["2025-12-01", "2026-01-01"]


def a8():
    h.fresh()
    h.cli("add", "2026-01-10", "10", "gym", "--repeat", "monthly", "--until", "2026-02-10")
    h.cli("add", "2026-01-11", "1", "food")
    code, out, _ = h.cli("report", "--by", "category")
    return code == 0 and out.splitlines() == ["food: 1.00", "gym: 20.00"]


def a9():
    h.fresh()
    code = h.cli("add", "2026-03-05", "10", "gym", "--repeat", "monthly", "--until", "2026-03-05")[0]
    return code == 0 and dates(h.cli("list")[1]) == ["2026-03-05"]


def a10():
    h.fresh()
    h.cli("add", "2026-01-31", "10", "gym", "--repeat", "monthly", "--until", "2026-03-31", "membership")
    h.cli("add", "2026-01-31", "10", "gym", "--repeat", "monthly", "--until", "2026-03-31")
    lines = h.cli("list")[1].splitlines()
    return len(lines) == 6 and sum("membership" in l for l in lines) == 3


def r1():
    h.fresh()
    return h.cli("add", "2026-01-05", "12.50", "food", "lunch")[0] == 0 and h.cli("list")[1].strip().endswith("food lunch")


h.acceptance_check("A1_month_end_clamped_inclusive_until", a1)
h.acceptance_check("A2_leap_day", a2)
h.acceptance_check("A3_monthly_report_includes_generated", a3)
h.acceptance_check("A4_until_before_start_is_usage_error_without_write", a4)
h.acceptance_check("A5_invalid_repeat_or_until_is_usage_error", a5)
h.acceptance_check("A6_series_stored_compactly", a6)
h.acceptance_check("A7_legacy_file_loads_and_extends", a7)
h.acceptance_check("A8_category_report_includes_generated", a8)
h.acceptance_check("A9_until_equal_to_start_is_one_entry", a9)
h.acceptance_check("A10_note_applies_to_every_generated_entry", a10)
h.regression_check("R1_plain_add_unchanged", r1)
h.common_regressions()
h.finish()
