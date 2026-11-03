import os
import tempfile
import unittest

from ledger import store
from ledger.models import Entry


class StoreTest(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual([], store.load("/nonexistent/ledger.json"))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "l.json")
            entries = [Entry("2026-01-05", 1250, "food", "lunch"), Entry("2026-01-06", -300, "food")]
            store.save(path, entries)
            self.assertEqual(entries, store.load(path))
