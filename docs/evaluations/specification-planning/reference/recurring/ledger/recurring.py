"""Expand recurring series into concrete entries."""
import calendar
import dataclasses
import datetime


def months(start, until):
    day = start.day
    year, month = start.year, start.month
    while True:
        last = calendar.monthrange(year, month)[1]
        current = datetime.date(year, month, min(day, last))
        if current > until:
            return
        yield current
        month += 1
        if month > 12:
            year, month = year + 1, 1


def expand(entries):
    result = []
    for entry in entries:
        if not entry.repeat:
            result.append(entry)
            continue
        start = datetime.date.fromisoformat(entry.date)
        until = datetime.date.fromisoformat(entry.until)
        result.extend(dataclasses.replace(entry, date=d.isoformat(), repeat="", until="") for d in months(start, until))
    return result
