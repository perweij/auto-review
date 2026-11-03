"""Ledger entries."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    date: str          # ISO date, YYYY-MM-DD
    amount_cents: int  # positive spending, negative refund
    category: str
    note: str = ""
    repeat: str = ""   # "" or "monthly"
    until: str = ""    # inclusive end date of a series

    def to_dict(self):
        value = {"date": self.date, "amount_cents": self.amount_cents, "category": self.category, "note": self.note}
        if self.repeat:
            value.update(repeat=self.repeat, until=self.until)
        return value

    @classmethod
    def from_dict(cls, value):
        return cls(value["date"], int(value["amount_cents"]), value["category"], value.get("note", ""),
                   value.get("repeat", ""), value.get("until", ""))
