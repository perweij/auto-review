"""Totals over ledger entries."""


def format_cents(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def totals_by_category(entries):
    totals = {}
    for entry in entries:
        key = entry.category.lower()
        totals[key] = totals.get(key, 0) + entry.amount_cents
    return dict(sorted(totals.items()))


def monthly_totals(entries):
    totals = {}
    for entry in entries:
        month = entry.date[:7]
        totals[month] = totals.get(month, 0) + entry.amount_cents
    return dict(sorted(totals.items()))
