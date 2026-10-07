"""No API calls: bounded feedback repair with truthful final audit status."""

import unittest
from unittest.mock import patch

import orchestrator as flow
from shared.audit import normalize_audit


def high_issue(kind="时间", actionable=True):
    return {"severity": "high", "type": kind,
            "detail": "18:00结束活动，18:15的列车来不及赶到。" if kind == "时间" else "已知4500超出用户总预算4000。",
            "suggestion": "提前结束活动并预留45分钟交通。" if kind == "时间" else "用已检索到的便宜酒店替换。",
            "actionable": actionable}


class AuditFlowTests(unittest.TestCase):
    def execute(self, audits):
        calls = []
        queue = list(audits)

        def stub_call(python, script, payload):
            calls.append((script.name, payload))
            if script == flow.SEARCH_PY:
                return {"destination": "杭州", "start_date": "2026-10-16", "end_date": "2026-10-17"}
            if script == flow.PLAN_PY:
                return {"destination": "杭州", "plans": [{"style": "推荐", "itinerary": []}], "blocks": []}
            if script == flow.VALIDATE_PY:
                return queue.pop(0)
            raise AssertionError("unexpected call")

        data = {"destination": "杭州", "start_date": "2026-10-16", "end_date": "2026-10-17",
                "basic": {"total_budget": 4000, "travelers": "2人"}}
        with patch.object(flow, "_call", side_effect=stub_call):
            result = flow.build_graph().invoke(data, {"configurable": {"thread_id": "test-review"}})
        return result, calls

    def test_actionable_time_problem_gets_one_feedback_replan(self):
        medium = {"severity": "medium", "detail": "餐厅价位可多样化", "suggestion": "可选更便宜餐厅"}
        result, calls = self.execute([
            {"passed": False, "issues": [high_issue(), medium], "feedback": "全部建议都必须调整"},
            {"passed": True, "issues": [medium], "feedback": "优化建议不影响执行"},
        ])
        self.assertEqual(result["iteration"], 2)
        self.assertEqual([name for name, _ in calls].count("search.py"), 1)
        self.assertEqual([name for name, _ in calls].count("plan.py"), 2)
        self.assertEqual([name for name, _ in calls].count("validate.py"), 2)
        second_plan = [payload for name, payload in calls if name == "plan.py"][1]
        self.assertIn("18:15", second_plan["feedback"])
        self.assertNotIn("餐厅价位可多样化", second_plan["feedback"])
        self.assertIs(result["audit"]["passed"], True)
        self.assertEqual(result["audit"]["issues"], normalize_audit({"passed": True, "issues": [medium]})["issues"])
        self.assertEqual([entry["passed"] for entry in result["history"]], [False, True])

    def test_unresolved_hard_budget_failure_stops_after_second_audit(self):
        audit = {"passed": False, "issues": [high_issue("预算")], "feedback": "总费用超预算"}
        result, _ = self.execute([audit, audit])
        self.assertEqual(result["iteration"], 2)
        self.assertIs(result["audit"]["passed"], False)
        self.assertEqual(len(result["history"]), 2)

    def test_medium_only_false_is_retained_without_replanning(self):
        audit = {"passed": False, "issues": [{"severity": "medium", "detail": "报价未知", "suggestion": "核实报价"}]}
        result, calls = self.execute([audit])
        self.assertEqual(result["iteration"], 1)
        self.assertIs(result["audit"]["passed"], False)
        self.assertEqual(result["audit"], normalize_audit(audit, result["plan"]))
        self.assertEqual(len(calls), 3)

    def test_soft_meal_budget_and_free_time_suggestions_do_not_loop(self):
        audit = {"passed": True, "issues": [
            {"severity": "low", "type": "预算", "detail": "单餐高于内部平均分摊，但用户没有单餐硬上限", "suggestion": "可以比较价位"},
            {"severity": "low", "type": "时间", "detail": "返程前可以休息和候车", "suggestion": "可保留留白"},
            {"severity": "medium", "type": "预算", "detail": "酒店报价待核实，整体预算unknown", "suggestion": "预订前核实", "actionable": False}]}
        result, _ = self.execute([audit])
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(result["audit"], normalize_audit(audit, result["plan"]))

    def test_non_actionable_high_needs_external_information_not_new_plan(self):
        audit = {"passed": False, "issues": [high_issue(actionable=False)]}
        result, _ = self.execute([audit])
        self.assertEqual(result["iteration"], 1)
        self.assertIs(result["audit"]["passed"], False)

    def test_high_without_specific_fix_does_not_repeat(self):
        result, _ = self.execute([{"passed": False, "issues": [{"severity": "high", "detail": "存在问题"}]}])
        self.assertEqual(result["iteration"], 1)
        self.assertIs(result["audit"]["passed"], False)

    def test_missing_verdict_or_error_never_defaults_to_passing(self):
        for audit in ({}, [], {"error": "timeout"}, {"passed": True, "error": "timeout"},
                      {"passed": False, "error": "timeout", "issues": [high_issue()]}):
            with self.subTest(audit=audit):
                result, _ = self.execute([audit])
                self.assertEqual(result["iteration"], 1)
                self.assertIs(result["audit"]["passed"], False)
                self.assertTrue(result["audit"]["error"])
                self.assertTrue(result["history"][0]["error"])


if __name__ == "__main__":
    unittest.main()
