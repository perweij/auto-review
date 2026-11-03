"""Command line: ``python -m ledger add|list|report``."""
import argparse
import datetime
import os
import sys

from . import export, report, store
from .models import Entry


def parse_amount(text):
    """Decimal text such as 12.5 -> 1250 cents."""
    whole, _, fraction = text.partition(".")
    return int(whole) * 100 + int((fraction + "00")[:2]) * (-1 if whole.startswith("-") else 1)


def build_parser():
    parser = argparse.ArgumentParser(prog="ledger")
    parser.add_argument("--data", default=os.environ.get("LEDGER_DATA", "ledger.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("date")
    add.add_argument("amount")
    add.add_argument("category")
    add.add_argument("note", nargs="?", default="")
    commands.add_parser("list")
    exporting = commands.add_parser("export")
    exporting.add_argument("--format", required=True, choices=export.FORMATS)
    exporting.add_argument("--output")
    exporting.add_argument("--force", action="store_true")
    summary = commands.add_parser("report")
    summary.add_argument("--by", choices=["category", "month"], default="category")
    return parser


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code
    entries = store.load(args.data)
    if args.command == "add":
        try:
            datetime.date.fromisoformat(args.date)
            cents = parse_amount(args.amount)
        except ValueError:
            print("error: invalid date or amount", file=err)
            return 2
        store.save(args.data, entries + [Entry(args.date, cents, args.category, args.note)])
        return 0
    if args.command == "list":
        for entry in sorted(entries, key=lambda e: e.date):
            print(f"{entry.date} {report.format_cents(entry.amount_cents):>10} {entry.category} {entry.note}".rstrip(), file=out)
        return 0
    if args.command == "export":
        text = export.render(entries, args.format)
        if args.output is None:
            out.write(text)
            return 0
        if os.path.exists(args.output) and not args.force:
            print(f"error: {args.output} exists; use --force to replace it", file=err)
            return 2
        try:
            with open(args.output, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc.strerror}", file=err)
            return 2
        return 0
    totals = report.totals_by_category(entries) if args.by == "category" else report.monthly_totals(entries)
    for key, cents in totals.items():
        print(f"{key}: {report.format_cents(cents)}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
