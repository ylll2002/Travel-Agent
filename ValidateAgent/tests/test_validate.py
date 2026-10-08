"""Pure stub tests: review conclusions and unavailable quotes remain truthful."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ValidateAgent.validate import _validate_model as validate_plan
from shared.audit import normalize_audit


def response(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class ValidateTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("ValidateAgent.validate.os.environ", {"OPENAI_API_KEY": "local-test-only"})
        environment.start()
        self.addCleanup(environment.stop)

    def model(self, contents):
        stub = patch("ValidateAgent.validate.OpenAI")
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
            with self.subTest(content=content), patch("ValidateAgent.validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = response(content)
                result = validate_plan({})
                self.assertIs(result["passed"], False)
                self.assertTrue(result["error"])
                self.assertEqual(result["status"], "error")
                self.assertEqual(factory.return_value.chat.completions.create.call_count, 2)

    def test_timeout_returns_failed_audit_without_retry_loop(self):
        with patch("ValidateAgent.validate.OpenAI") as factory:
            factory.return_value.chat.completions.create.side_effect = TimeoutError("test timeout")
            result = validate_plan({})
            self.assertIs(result["passed"], False)
            self.assertTrue(result["error"])
            self.assertEqual(factory.return_value.chat.completions.create.call_count, 1)

    def test_missing_model_configuration_never_passes(self):
        with patch("ValidateAgent.validate.OpenAI", side_effect=ValueError("missing key")):
            result = validate_plan({})
        self.assertIs(result["passed"], False)
        self.assertTrue(result["error"])

    def test_missing_api_key_is_explicit_and_never_reports_a_passing_review(self):
        for key in (None, "", "   ", "sk-your-key-here"):
            with self.subTest(key=key), patch.dict("ValidateAgent.validate.os.environ", {} if key is None else {"OPENAI_API_KEY": key}, clear=True), patch("ValidateAgent.validate.OpenAI") as constructor:
                result = validate_plan({"revision": 7, "blocks": []})
            self.assertIn("未配置模型 API Key", result["error"])
            self.assertFalse(result["passed"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["plan_revision"], 7)
            constructor.assert_not_called()

    def test_invalid_issue_or_contradictory_verdict_is_retried(self):
        invalid = [
            {"passed": True, "issues": ["bad"]},
            {"passed": True, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改"}]},
            {"passed": False, "issues": [{"severity": "high", "detail": "冲突"}]},
            {"passed": False, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改", "block_ids": ["invented"]}]},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), patch("ValidateAgent.validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = response(json.dumps(raw))
                result = validate_plan({})
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["passed"])
                self.assertEqual(factory.return_value.chat.completions.create.call_count, 2)

    def test_retry_explains_invalid_location_and_keeps_same_input(self):
        bad = {"passed": False, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改", "block_ids": ["invented"]}]}
        client = self.model([json.dumps(bad), json.dumps({"passed": True, "issues": []})])
        result = validate_plan({"revision": 3, "blocks": []})
        self.assertTrue(result["passed"])
        first, second = client.chat.completions.create.call_args_list
        self.assertEqual(first.kwargs["messages"][:2], second.kwargs["messages"][:2])
        self.assertEqual(len(first.kwargs["messages"]), 2)
        self.assertIn("位置", second.kwargs["messages"][-1]["content"])
        self.assertIn("暂无报价", second.kwargs["messages"][-1]["content"])

    def test_truncated_reply_gets_one_larger_complete_retry(self):
        truncated = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
            message=SimpleNamespace(content='{"passed":true,"issues":['))])
        with patch("ValidateAgent.validate.OpenAI") as factory:
            client = factory.return_value
            client.chat.completions.create.side_effect = [truncated, response('{"passed":true,"issues":[]}')]
            result = validate_plan({"revision": 3})
        self.assertTrue(result["passed"])
        self.assertEqual(client.chat.completions.create.call_count, 2)
        calls = client.chat.completions.create.call_args_list
        self.assertGreater(calls[1].kwargs["max_tokens"], calls[0].kwargs["max_tokens"])
        self.assertIn("截断", calls[1].kwargs["messages"][-1]["content"])

    def test_repeated_invalid_json_reports_specific_failure_not_quote_block(self):
        client = self.model(['{"passed":', '{"passed":'])
        result = validate_plan({})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "模型审核未返回有效JSON")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_invalid_response_can_be_repaired_once(self):
        verdict = {"passed": True, "issues": []}
        client = self.model(["not JSON", json.dumps(verdict)])
        self.assertEqual(validate_plan({}), normalize_audit(verdict))
        self.assertEqual(client.chat.completions.create.call_count, 2)


    def test_compact_paths_expand_from_input_without_copying_model_values(self):
        plan = {"revision": 3, "blocks": [{"id": "b6", "day": 1, "name": "宋城",
                "note": "较长说明" * 1000, "unit_price": None, "price_known": False}]}
        raw = {"passed": True, "issues": [{"severity": "medium", "type": "预算",
               "detail": "报价待核实", "suggestion": "预订前核实", "actionable": False,
               "block_ids": ["b6"], "evidence": ["plan.blocks[0].unit_price", "plan.blocks[0].price_known"]}]}
        client = self.model([json.dumps(raw)])
        result = validate_plan(plan)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["issues"][0]["evidence"], [
            {"path": "plan.blocks[0].unit_price", "value": None},
            {"path": "plan.blocks[0].price_known", "value": False}])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(raw["issues"][0]["evidence"][0], "plan.blocks[0].unit_price")
        self.assertLess(len(json.dumps(raw)), 500)

    def test_missing_or_overly_broad_evidence_paths_require_another_complete_reply(self):
        for path in ("plan.blocks", "plan.blocks[99].name", "plan.__class__", "rule_review.issues[0].detail"):
            with self.subTest(path=path):
                raw = {"passed": False, "issues": [{"severity": "high", "detail": "冲突",
                       "suggestion": "调整", "evidence": [path]}]}
                client = self.model([json.dumps(raw), json.dumps({"passed": True, "issues": []})])
                result = validate_plan({"blocks": [{"name": "宋城", "options": []}]})
                self.assertTrue(result["passed"])
                self.assertEqual(client.chat.completions.create.call_count, 2)
                self.assertIn("依据路径", client.chat.completions.create.call_args.kwargs["messages"][-1]["content"])

    def test_common_path_notation_and_path_only_records_resolve_exact_values(self):
        plan = {"blocks": [{"name": "宋城", "options": [], "price_known": False}]}
        paths = ["  `$.plan.blocks[0].name`  ", "plan['blocks'][0]['price_known']",
                 {"path": "plan.blocks[0].options"}]
        raw = {"passed": True, "issues": [{"severity": "medium", "detail": "需核实",
               "suggestion": "确认报价", "evidence": paths}]}
        client = self.model([json.dumps(raw)])
        result = validate_plan(plan)
        self.assertNotIn("error", result)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(result["issues"][0]["evidence"], [
            {"path": "plan.blocks[0].name", "value": "宋城"},
            {"path": "plan.blocks[0].price_known", "value": False},
            {"path": "plan.blocks[0].options", "value": []}])

    def test_concrete_object_evidence_expands_without_changing_verdict(self):
        plan = {"blocks": [{"name": "宋城", "note": "闭园", "options": [{"name": "替代地点"}]}]}
        raw = {"passed": False, "issues": [{"severity": "high", "detail": "地点闭园",
               "suggestion": "更换地点", "evidence": ["plan.blocks[0]"]}]}
        client = self.model([json.dumps(raw)])
        result = validate_plan(plan)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["issues"][0]["evidence"], [
            {"path": "plan.blocks[0].name", "value": "宋城"},
            {"path": "plan.blocks[0].note", "value": "闭园"},
            {"path": "plan.blocks[0].options[0].name", "value": "替代地点"}])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_wrong_weather_shape_retry_contains_actual_paths(self):
        def verdict(path):
            return {"passed": False, "issues": [{"severity": "high", "type": "天气",
                    "detail": "暴雨", "suggestion": "换室内活动", "evidence": [path]}]}
        client = self.model([json.dumps(verdict("search.weather.days[0].weather")),
                             json.dumps(verdict("search.weather[0].weather"))])
        result = validate_plan({}, search={"weather": [{"weather": "暴雨"}]})
        self.assertEqual(result["status"], "blocked")
        retry = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertIn('search.weather.days[0].weather', retry)
        self.assertIn('search.weather[0].weather', retry)
        first_prompt = client.chat.completions.create.call_args_list[0].kwargs["messages"][0]["content"]
        self.assertIn('search.weather[0].weather', first_prompt)
        self.assertNotIn('search.weather.days[0].weather', first_prompt)
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_bounded_weather_container_resolves_and_large_container_requests_specific_fields(self):
        raw = {"passed": False, "issues": [{"severity": "high", "type": "天气", "detail": "暴雨",
               "suggestion": "换室内", "evidence": ["search.weather"]}]}
        client = self.model([json.dumps(raw)])
        result = validate_plan({}, search={"weather": [{"weather": "暴雨"}]})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["issues"][0]["evidence"], [{"path": "search.weather[0].weather", "value": "暴雨"}])
        self.assertEqual(client.chat.completions.create.call_count, 1)
        fixed = json.loads(json.dumps(raw))
        fixed["issues"][0]["evidence"] = ["search.weather[0].weather"]
        client = self.model([json.dumps(raw), json.dumps(fixed)])
        result = validate_plan({}, search={"weather": [{"weather": "暴雨"}] * 100})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_repeated_missing_path_never_silently_passes(self):
        raw = {"passed": False, "issues": [{"severity": "high", "detail": "闭园",
               "suggestion": "替换", "evidence": ["plan.blocks[9].opening_hours"]}]}
        client = self.model([json.dumps(raw), json.dumps(raw)])
        result = validate_plan({"blocks": [{"name": "宋城"}]})
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["passed"])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_repeated_truncation_never_accepts_even_an_apparently_complete_verdict(self):
        complete_looking = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
            message=SimpleNamespace(content='{"passed":true,"issues":[]}'))])
        with patch("ValidateAgent.validate.OpenAI") as factory:
            client = factory.return_value
            client.chat.completions.create.return_value = complete_looking
            result = validate_plan({"revision": 3})
        self.assertFalse(result["passed"])
        self.assertEqual(result["error"], "模型审核回复被截断")
        self.assertEqual(client.chat.completions.create.call_count, 2)



if __name__ == "__main__":
    unittest.main()
