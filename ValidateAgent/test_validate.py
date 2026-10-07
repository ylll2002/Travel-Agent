"""Pure stub tests: review conclusions and unavailable quotes remain truthful."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from validate import _validate_model as validate_plan
from shared.audit import normalize_audit


def response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class ValidateTests(unittest.TestCase):
    def model(self, contents):
        stub = patch("validate.OpenAI")
        client_class = stub.start()
        self.addCleanup(stub.stop)
        client = client_class.return_value
        client.chat.completions.create.side_effect = [response(content) for content in contents]
        return client

    def test_unknown_hotel_quote_and_soft_meal_budget_do_not_rewrite_verdict(self):
        plan = {"cost_by_style": {"推荐": 1096}, "budget_by_style": {"推荐": "unknown"},
                "unpriced_items": {"推荐": ["酒店", "高铁"]},
                "blocks": [{"name": "附近餐厅", "type": "美食", "price": 310,
                            "unit_price": 155, "price_basis": "per_person", "over_meal_budget": True},
                           {"name": "酒店", "price": 0, "unit_price": None, "price_known": False}]}
        basic = {"total_budget": 4000, "travelers": "2人", "notes": "节奏轻松"}
        verdict = {"passed": True, "issues": [{"severity": "medium", "type": "预算",
                   "detail": "酒店与高铁报价未知，预订前核实总费用。", "suggestion": "核实报价", "actionable": False}],
                   "feedback": "可执行，预算状态未知。"}
        client = self.model([json.dumps(verdict, ensure_ascii=False)])
        self.assertEqual(validate_plan(plan, basic=basic), normalize_audit(verdict, plan))
        sent = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["plan"], plan)
        self.assertEqual(sent["basic"], basic)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_real_hard_budget_violation_remains_failed(self):
        verdict = {"passed": False, "issues": [{"severity": "high", "type": "预算",
                   "detail": "已知费用4500超过用户总预算4000。", "suggestion": "替换为更便宜的酒店", "actionable": True}],
                   "feedback": "降低已知总费用。"}
        self.model([json.dumps(verdict)])
        result = validate_plan({"cost_by_style": {"推荐": 4500}}, basic={"total_budget": 4000})
        self.assertEqual(result, normalize_audit(verdict))

    def test_time_conflict_remains_failed(self):
        verdict = {"passed": False, "issues": [{"severity": "high", "type": "时间",
                   "detail": "景点18:00结束，18:15火车发车，交通需要45分钟。", "suggestion": "将景点结束时间提前至17:00", "actionable": True}],
                   "feedback": "预留真实交通及进站缓冲。"}
        self.model([json.dumps(verdict)])
        self.assertEqual(validate_plan({"blocks": [], "legs": [{"duration_s": 2700}]}), normalize_audit(verdict))

    def test_medium_only_false_is_not_silently_changed_to_true(self):
        verdict = {"passed": False, "issues": [{"severity": "medium", "detail": "报价待核实", "suggestion": "核实"}]}
        self.model([json.dumps(verdict)])
        self.assertEqual(validate_plan({}), normalize_audit(verdict))

    def test_malformed_or_missing_boolean_never_passes(self):
        for content in ("not JSON", "[]", "null", "{}", '{"passed":"true"}', '{"passed":true,"issues":{}}'):
            with self.subTest(content=content), patch("validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = response(content)
                result = validate_plan({})
                self.assertIs(result["passed"], False)
                self.assertTrue(result["error"])
                self.assertEqual(result["status"], "error")
                self.assertEqual(factory.return_value.chat.completions.create.call_count, 2)

    def test_timeout_returns_failed_audit_without_retry_loop(self):
        with patch("validate.OpenAI") as factory:
            factory.return_value.chat.completions.create.side_effect = TimeoutError("test timeout")
            result = validate_plan({})
            self.assertIs(result["passed"], False)
            self.assertTrue(result["error"])
            self.assertEqual(factory.return_value.chat.completions.create.call_count, 1)

    def test_missing_model_configuration_never_passes(self):
        with patch("validate.OpenAI", side_effect=ValueError("missing key")):
            result = validate_plan({})
        self.assertIs(result["passed"], False)
        self.assertTrue(result["error"])

    def test_invalid_issue_or_contradictory_verdict_is_retried(self):
        invalid = [
            {"passed": True, "issues": ["bad"]},
            {"passed": True, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改"}]},
            {"passed": False, "issues": [{"severity": "high", "detail": "冲突"}]},
            {"passed": False, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改", "block_ids": ["invented"]}]},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), patch("validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = response(json.dumps(raw))
                result = validate_plan({})
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["passed"])
                self.assertEqual(factory.return_value.chat.completions.create.call_count, 2)

    def test_invalid_response_can_be_repaired_once(self):
        verdict = {"passed": True, "issues": []}
        client = self.model(["not JSON", json.dumps(verdict)])
        self.assertEqual(validate_plan({}), normalize_audit(verdict))
        self.assertEqual(client.chat.completions.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
