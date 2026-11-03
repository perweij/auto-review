import sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import Harness

h = Harness(sys.argv[1])


def seed():
    h.fresh()
    for date, amount, category in (("2026-01-04", "4", "d"), ("2026-01-02", "2", "b"), ("2026-01-01", "1", "a"), ("2026-01-03", "3", "c")):
        assert h.cli("add", date, amount, category)[0] == 0


def dates(out):
    return [line.split()[0] for line in out.splitlines()]


def a1():
    seed()
    code, out, _ = h.cli("list", "--limit", "2")
    return code == 0 and dates(out) == ["2026-01-03", "2026-01-04"]


def a2():
    seed()
    code, out, _ = h.cli("list", "--limit", "10")
    return code == 0 and dates(out) == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]


def a3():
    seed()
    results = []
    for bad in ("0", "-1", "abc", "1.5", ""):
        code, out, err = h.cli("list", "--limit", bad)
        results.append(code == 2 and out == "" and err.strip() != "")
    return all(results)


def a4():
    seed()
    code, out, _ = h.cli("list", "--limit", "1")
    return code == 0 and dates(out) == ["2026-01-04"]


def r1():
    seed()
    code, out, _ = h.cli("list")
    return code == 0 and dates(out) == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]


h.acceptance_check("A1_latest_n_oldest_first", a1)
h.acceptance_check("A2_limit_above_count_shows_all", a2)
h.acceptance_check("A3_non_positive_or_invalid_is_usage_error", a3)
h.acceptance_check("A4_limit_one", a4)
h.regression_check("R1_no_limit_unchanged", r1)
h.common_regressions()
h.finish()
