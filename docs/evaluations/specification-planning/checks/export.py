import csv
import io
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from common import Harness

h = Harness(sys.argv[1])


def seed():
    h.fresh()
    for argv in (("2026-01-04", "4.5", "Food", "late, \"quoted\" lunch"), ("2026-01-02", "-3", "rent"), ("2026-01-03", "12.25", "food")):
        assert h.cli("add", *argv)[0] == 0


def a1():
    seed()
    code, out, _ = h.cli("export", "--format", "csv")
    rows = list(csv.reader(io.StringIO(out)))
    return code == 0 and rows[0] == ["date", "amount", "category", "note"] and [r[0] for r in rows[1:]] == ["2026-01-02", "2026-01-03", "2026-01-04"] \
        and [r[1] for r in rows[1:]] == ["-3.00", "12.25", "4.50"]


def a2():
    seed()
    code, out, _ = h.cli("export", "--format", "json")
    value = json.loads(out)
    entries = value.get("entries", []) if isinstance(value, dict) else []
    return code == 0 and value.get("version") == 1 and [e.get("date") for e in entries] == ["2026-01-02", "2026-01-03", "2026-01-04"] \
        and entries[0] == {"date": "2026-01-02", "amount": "-3.00", "category": "rent", "note": ""}


def a3():
    seed()
    target = os.path.join(h.directory, "out.csv")
    code, out, _ = h.cli("export", "--format", "csv", "--output", target)
    return code == 0 and out == "" and open(target, encoding="utf-8").read().splitlines()[0] == "date,amount,category,note"


def a4():
    seed()
    target = os.path.join(h.directory, "out.json")
    with open(target, "w", encoding="utf-8") as stream:
        stream.write("PRECIOUS")
    code, _, err = h.cli("export", "--format", "json", "--output", target)
    kept = open(target, encoding="utf-8").read() == "PRECIOUS"
    forced = h.cli("export", "--format", "json", "--output", target, "--force")[0] == 0
    return code == 2 and err.strip() != "" and kept and forced and json.load(open(target, encoding="utf-8"))["version"] == 1


def a5():
    seed()
    code, out, err = h.cli("export", "--format", "xml")
    return code == 2 and out == "" and err.strip() != "" and "Traceback" not in err


def a6():
    seed()
    code, out, err = h.cli("export", "--format", "csv", "--output", os.path.join(h.directory, "missing-dir", "x.csv"))
    return code == 2 and err.strip() != "" and "Traceback" not in err


def a7():
    seed()
    code, out, err = h.cli("export")
    return code == 2 and out == "" and err.strip() != ""


def a8():
    seed()
    code, out, _ = h.cli("export", "--format", "csv")
    rows = list(csv.reader(io.StringIO(out)))
    return code == 0 and rows[3][3] == 'late, "quoted" lunch'


def r1():
    seed()
    code, out, _ = h.cli("report")
    return code == 0 and out.splitlines() == ["Food: 4.50", "food: 12.25", "rent: -3.00"]


h.acceptance_check("A1_csv_stdout_header_sorted_decimals", a1)
h.acceptance_check("A2_json_object_with_version_and_entries", a2)
h.acceptance_check("A3_output_file_replaces_stdout", a3)
h.acceptance_check("A4_existing_file_refused_unless_force", a4)
h.acceptance_check("A5_unknown_format_usage_error", a5)
h.acceptance_check("A6_unwritable_path_usage_error", a6)
h.acceptance_check("A7_format_is_required", a7)
h.acceptance_check("A8_csv_quoting", a8)
h.regression_check("R1_report_still_works", r1)
h.common_regressions()
h.finish()
