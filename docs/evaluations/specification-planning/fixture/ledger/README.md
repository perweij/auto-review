# ledger

A small expense ledger. Entries live in a JSON file (`--data` or `$LEDGER_DATA`).

    python -m ledger add 2026-01-05 12.50 food "lunch"
    python -m ledger list
    python -m ledger report --by month

Tests: `python -m unittest discover -s tests`.
