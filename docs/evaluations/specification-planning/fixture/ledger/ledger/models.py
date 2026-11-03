"""Ledger entries."""
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Entry:
    date: str          # ISO date, YYYY-MM-DD
    amount_cents: int  # positive spending, negative refund
    category: str
    note: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(value["date"], int(value["amount_cents"]), value["category"], value.get("note", ""))
