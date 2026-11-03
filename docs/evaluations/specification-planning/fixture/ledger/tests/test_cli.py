import io
import os
import tempfile
import unittest

from ledger import cli


class CliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = os.path.join(self.directory.name, "ledger.json")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--data", self.data, *argv], out, err)
        return code, out.getvalue(), err.getvalue()

    def test_add_list_report(self):
        self.assertEqual(0, self.run_cli("add", "2026-01-05", "12.50", "food", "lunch")[0])
        self.assertEqual(0, self.run_cli("add", "2026-01-03", "30", "rent")[0])
        code, out, _ = self.run_cli("list")
        self.assertEqual(["2026-01-03      30.00 rent", "2026-01-05      12.50 food lunch"], out.splitlines())
        self.assertEqual("food: 12.50\nrent: 30.00\n", self.run_cli("report")[1])
        self.assertEqual("2026-01: 42.50\n", self.run_cli("report", "--by", "month")[1])

    def test_invalid_input_exits_2(self):
        code, _, err = self.run_cli("add", "2026-13-01", "5", "food")
        self.assertEqual(2, code)
        self.assertIn("invalid", err)
        self.assertEqual(2, self.run_cli("add", "2026-01-01", "abc", "food")[0])
