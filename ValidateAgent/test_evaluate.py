"""Fixed-case labels and the report must detect regressions, not bless every output."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ValidateAgent import evaluate as evaluation


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(evaluation.DATASET.read_text())

    def test_all_fixed_labels_and_input_evidence_are_reproducible(self):
        original = deepcopy(self.dataset)
        report = evaluation.evaluate(self.dataset)
        self.assertEqual(report["metrics"]["passed_cases"], len(self.dataset["cases"]))
        self.assertEqual(report["metrics"]["high_false_positive"], 0)
        self.assertEqual(report["metrics"]["high_false_negative"], 0)
        self.assertEqual(report["metrics"]["evidence_count"], report["metrics"]["verifiable_evidence_count"])
        self.assertEqual(report, evaluation.evaluate(self.dataset))
        self.assertEqual(self.dataset, original)

    def test_wrong_ground_truth_does_not_silently_pass(self):
        case = deepcopy(self.dataset["cases"][0])
        case["expected"] = {"status": "blocked", "high": [{"type":"预算", "plan_style":"经典", "block_ids":["a"], "source":"rule"}], "warning_types": []}
        report = evaluation.evaluate({"cases": [case]})
        self.assertEqual(report["metrics"]["passed_cases"], 0)
        self.assertEqual(report["metrics"]["high_false_negative"], 1)
        self.assertEqual(report["metrics"]["missed_blocked_cases"], 1)

    def test_duplicate_ids_are_rejected(self):
        self.dataset["cases"].append(deepcopy(self.dataset["cases"][0]))
        with self.assertRaises(ValueError):
            evaluation.evaluate(self.dataset)

    def test_stdlib_only_cli_and_check_detect_report_tampering(self):
        script = Path(evaluation.__file__)
        with tempfile.TemporaryDirectory() as folder:
            command = [sys.executable, "-S", str(script), "--output-dir", folder]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run([*command, "--check"], capture_output=True).returncode, 0)
            report = Path(folder) / "report.md"
            report.write_text(report.read_text() + "changed")
            self.assertEqual(subprocess.run([*command, "--check"], capture_output=True).returncode, 1)
            self.assertTrue(report.read_text().endswith("changed"), "--check must not modify files")


if __name__ == "__main__":
    unittest.main()
