"""Entry export formats."""
import csv
import io
import json

from . import report

FORMATS = ("csv", "json")


def rows(entries):
    return [{"date": e.date, "amount": report.format_cents(e.amount_cents), "category": e.category, "note": e.note}
            for e in sorted(entries, key=lambda e: e.date)]


def render(entries, fmt):
    if fmt == "json":
        return json.dumps({"version": 1, "entries": rows(entries)}, indent=2) + "\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, ["date", "amount", "category", "note"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows(entries))
    return stream.getvalue()
