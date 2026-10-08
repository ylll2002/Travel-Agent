"""Contract tests exercise malformed responses and plan-location validation."""
import copy
import unittest
from ValidateAgent import validate
from shared.audit import failed_audit, high_actionable_issues, history_entry, normalize_audit


class AuditContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = {"revision": 4, "blocks": [
            {"id": "a", "plan_style": "经典", "day": 1, "date": "2026-10-10"},
            {"id": "b", "plan_style": "探索", "day": 2, "date": "2026-10-11"}]}
        self.issue = {"severity": "high", "type": "时间", "detail": "活动重叠", "suggestion": "提前结束",
                      "actionable": True, "plan_style": "经典", "day": 1, "date": "2026-10-10", "block_ids": ["a"],
                      "evidence": [{"path": "plan.blocks[0].time", "value": "09:00-11:00"}]}

    def test_status_and_legacy_fields(self):
        for passed, issues, status in ((True, [], "passed"), (True, [{**self.issue, "severity": "low"}], "warning"),
                                       (False, [self.issue], "blocked")):
            with self.subTest(status=status):
                result = normalize_audit({"passed": passed, "issues": issues}, self.plan)
                self.assertEqual(result["status"], status)
                self.assertIs(result["passed"], passed)
                self.assertEqual(result["schema_version"], 1)
                self.assertEqual(result["plan_revision"], 4)
                self.assertEqual(result["reviewed_styles"], ["经典", "探索"])

    def test_locations_and_evidence_preserved_without_mutating_input(self):
        raw = {"passed": False, "issues": [self.issue]}
        original = copy.deepcopy(raw)
        result = normalize_audit(raw, self.plan)
        self.assertEqual(result["issues"][0]["block_ids"], ["a"])
        self.assertEqual(result["issues"][0]["evidence"], self.issue["evidence"])
        result["issues"][0]["evidence"][0]["value"] = "changed"
        self.assertEqual(raw, original)

    def test_reject_wrong_style_day_date_or_block(self):
        for change in ({"plan_style": "不存在"}, {"block_ids": ["b"]}, {"day": 2},
                       {"date": "2026-10-11"}, {"date": "not a date"}, {"day": True},
                       {"block_ids": ["a", "a"]}, {"block_ids": "a"}, {"evidence": [{}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_audit({"passed": False, "issues": [{**self.issue, **change}]}, self.plan)

    def test_old_issues_do_not_get_invented_location_or_auto_repair(self):
        result = normalize_audit({"passed": False, "issues": [
            {"severity": "medium", "detail": "报价未知", "suggestion": "核实"}]}, self.plan)
        issue = result["issues"][0]
        self.assertIsNone(issue["plan_style"])
        self.assertEqual(issue["block_ids"], [])
        self.assertEqual(issue["evidence"], [])
        self.assertFalse(issue["actionable"])
        self.assertFalse(result["passed"])

    def test_model_cannot_claim_rule_source_or_wrong_revision(self):
        result = normalize_audit({"passed": False, "plan_revision": 99, "issues": [
            {**self.issue, "source": "rule"}]}, self.plan)
        self.assertEqual(result["plan_revision"], 4)
        self.assertEqual(result["issues"][0]["source"], "model")
        self.assertEqual(normalize_audit({"passed": False, "issues": [self.issue]}, self.plan, source="rule")["issues"][0]["source"], "rule")

    def test_nested_plan_can_locate_day_without_flat_blocks(self):
        plan = {"plans": [{"style": "经典", "itinerary": [{"day": 1, "date": "2026-10-10", "schedule": [{"id": "a"}]}]}]}
        self.assertEqual(normalize_audit({"passed": False, "issues": [self.issue]}, plan)["issues"][0]["block_ids"], ["a"])

    def test_error_cannot_pass_and_keeps_review_context(self):
        raw = {"passed": True, "issues": [], "error": "timeout"}
        result = normalize_audit(raw, self.plan)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["passed"])
        self.assertEqual(result["plan_revision"], 4)
        self.assertEqual(high_actionable_issues(result), [])
        self.assertEqual(failed_audit("timeout", "retry", self.plan)["reviewed_styles"], ["经典", "探索"])

    def test_history_keeps_verdict_location_and_evidence(self):
        result = normalize_audit({"passed": False, "issues": [self.issue]}, self.plan)
        entry = history_entry(2, result)
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["issues"], result["issues"])
        self.assertEqual(entry["plan_revision"], 4)
        self.assertEqual(entry["iteration"], 2)

    def test_version_and_issue_types_are_strict(self):
        for raw in ({"passed": True, "issues": [], "schema_version": 2},
                    {"passed": True, "issues": [], "schema_version": True},
                    {"passed": "true", "issues": []}, {"passed": True},
                    {"passed": False, "issues": [{**self.issue, "actionable": "false"}]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_audit(raw, self.plan)


if __name__ == "__main__":
    unittest.main()
