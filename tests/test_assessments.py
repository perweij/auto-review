"""Severity extraction must not invent ratings or mistake confidence for impact."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from pr_review_api.assessments import comment_assessment, finding_assessments, report_severities


class AssessmentTest(unittest.TestCase):
    def test_explicit_legacy_headers_and_labels(self):
        report = """## Validated findings
**F1 — Medium, blocking — CONFIRMED (Claude).** `lib/example.sh:13`
High confidence in this reproduction.
### F2: Another finding
- **Severity:** Low
### F10 — Critical — CONFIRMED
## Coverage
Severity: High
"""
        self.assertEqual(report_severities(report, {"F1", "F2", "F10"}),
                         {"F1": "Medium", "F2": "Low", "F10": "Critical"})

    def test_explicit_table_columns_are_required(self):
        report = """| ID | Confidence | Severity |
| --- | --- | --- |
| F1 | High | **Low** |
| F2 | Low | Critical |
| ID | Source | Confidence |
| F3 | Claude | High |
"""
        self.assertEqual(report_severities(report, {"F1", "F2", "F3"}), {"F1": "Low", "F2": "Critical"})

    def test_prose_confidence_and_code_do_not_assign_a_rating(self):
        report = """### F1 — High confidence in the finding
The code processes Critical alerts. Low confidence is not low severity.
### F2 — Finding without an impact assessment
```text
**F2 — Critical, blocking**
Severity: High
```
> **F2 — High, blocking**
    **F2 — Critical, blocking**
### Other coverage
Severity: Medium
F3 is discussed elsewhere.
Severity: High
"""
        self.assertEqual(report_severities(report, {"F1", "F2", "F3"}), {})

    def test_conflicting_legacy_ratings_remain_unknown(self):
        report = "**F1 — High, blocking**\n**F1 — Low, non-blocking**\n**F2 — Medium**\n**F2 — Medium**"
        self.assertEqual(report_severities(report, {"F1", "F2"}), {"F2": "Medium"})

    def test_structured_rating_wins_and_invalid_is_not_guessed(self):
        data = {"findings": [{"id": "F1", "severity": "Low", "blocking": True},
                             {"id": "F2", "severity": "Urgent", "blocking": False},
                             {"id": "F3", "blocking": False}],
                "report_md": "**F1 — High**\n**F2 — Medium**\n**F3 — Critical**"}
        original = copy.deepcopy(data)
        values = finding_assessments(data)
        self.assertEqual(values["F1"]["severity"], "Low")
        self.assertEqual(values["F1"]["severity_source"], "structured")
        self.assertTrue(values["F1"]["blocking"])
        self.assertIsNone(values["F2"]["severity"])
        self.assertEqual(values["F3"]["severity_source"], "report")
        self.assertEqual(data, original)

    def test_grouped_findings_use_highest_known_rating_and_expose_gaps(self):
        findings = finding_assessments({"findings": [{"id": "F1", "severity": "Low"},
                                                    {"id": "F2", "severity": "High"}]})
        complete = comment_assessment({"finding_ids": ["F1", "F2"]}, findings)
        self.assertEqual(complete["severity"], "High")
        self.assertFalse(complete["severity_incomplete"])
        partial = comment_assessment({"finding_ids": ["F1", "F2", "missing"]}, findings)
        self.assertEqual(partial["severity"], "High")
        self.assertTrue(partial["severity_incomplete"])
        self.assertIsNone(partial["finding_assessments"][-1]["blocking"])
        self.assertIsNone(comment_assessment({"finding_ids": []}, findings)["severity"])

    def test_new_output_requires_valid_severity_but_legacy_output_remains_readable(self):
        data = {"recommendation": "COMMENT", "recommendation_rationale": "Advisory", "blocking_findings": 0,
                "report_md": "Saved report", "review_body_md": "Saved body",
                "findings": [{"id": "F1", "title": "Finding", "validity": "CONFIRMED",
                              "evidence": "Observed", "blocking": False, "body_md": "Exact text"}]}

        def valid(strict):
            args = ["jq", "-e", "--argjson", "require_severity", json.dumps(strict),
                    "-f", str(ROOT / "prompts/validate-synthesis.jq")]
            result = subprocess.run(args, input=json.dumps(data), text=True, capture_output=True)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            return result.returncode == 0

        self.assertTrue(valid(False))
        self.assertFalse(valid(True))
        for level in ("Critical", "High", "Medium", "Low"):
            data["findings"][0]["severity"] = level
            self.assertTrue(valid(True))
        for level in ("Urgent", "high", None, 0):
            data["findings"][0]["severity"] = level
            self.assertFalse(valid(True))
            self.assertFalse(valid(False))
