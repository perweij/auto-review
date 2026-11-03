"""JSON file storage. The file holds a list of entry objects."""
import json
import os

from .models import Entry


def load(path):
    """Return all entries; a missing file is an empty ledger."""
    try:
        with open(path, encoding="utf-8") as stream:
            return [Entry.from_dict(item) for item in json.load(stream)]
    except FileNotFoundError:
        return []


def save(path, entries):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump([entry.to_dict() for entry in entries], stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
