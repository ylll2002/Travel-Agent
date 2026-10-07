"""Actual rules plus stubbed SDK: union, provenance, evidence and failure paths."""
import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import validate
import run
from ValidateAgent.rules import validate_rules
from ValidateAgent.evidence import read_path, same_value
from ValidateAgent.context import plan_for_model, search_for_model
from shared.audit import combine_audits, failed_audit, history_entry, normalize_audit, repair_feedback
from shared.sources import merge_plan_sources


def fixture(price=30):
    plan = {"revision": 3, "blocks": [{"id": "v1", "plan_style": "经典", "day": 1,
            "date": "2026-10-10", "name": "博物馆", "type": "景点", "time": "09:00-11:00",
            "price": price, "price_known": True, "link": "https://example.test/museum"}]}
    search = {"poi": [{"name": "博物馆", "url": "https://example.test/museum", "opening_hours": "10:00-17:00"}],
              "weather": {"days": [{"date": "2026-10-10", "weather": "暴雨"}]}}
    return plan, search, {"total_budget": 100}


def model_issue(kind="天气", evidence=None):
    return {"severity": "high", "type": kind, "detail": "暴雨期间不宜安排露天活动",
            "suggestion": "替换为已有室内候选", "actionable": True, "block_ids": ["v1"],
            "plan_style": "经典", "day": 1, "date": "2026-10-10",
            "evidence": evidence if evidence is not None else [
                {"path": "search.weather.days[0].weather", "value": "暴雨"}]}


class HybridTests(unittest.TestCase):
    def model(self, raw):
        factory = patch("validate.OpenAI")
        stub = factory.start()
        self.addCleanup(factory.stop)
        client = stub.return_value
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw, ensure_ascii=False)))])
        return stub, client

    def test_rule_budget_block_skips_model_and_preserves_evidence(self):
        plan, search, basic = fixture(120)
        with patch("validate.OpenAI") as factory:
            result = validate.validate_plan(plan, search=search, basic=basic)
        factory.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"], {"rules": "completed", "model": "skipped"})
        self.assertEqual(result["rule_summary"]["budget_by_style"]["经典"]["known_cost"], 120)
        self.assertTrue(all(i["source"] == "rule" for i in result["issues"]))
        self.assertIn("120", repair_feedback(result))

    def test_model_pass_cannot_erase_hard_rule_finding(self):
        plan, search, basic = fixture(120)
        rule = validate_rules(plan, search, basic)
        original = copy.deepcopy(rule)
        result = combine_audits(rule, {"passed": True, "issues": []}, plan)
        self.assertFalse(result["passed"])
        self.assertEqual(result["issues"], rule["issues"])
        self.assertEqual(rule, original)

    def test_complete_candidates_are_used_before_model_trimming(self):
        plan, search, basic = fixture()
        search["poi"][0]["name"] = "其他博物馆"
        with patch("validate.OpenAI") as factory:
            result = validate.validate_plan(plan, search=search, basic=basic)
        factory.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(i["type"] == "真实性" and i["severity"] == "high" for i in result["issues"]))

    def test_invalid_rule_input_never_initializes_model(self):
        with patch("validate.OpenAI") as factory:
            result = validate.validate_plan({"blocks": 123})
        factory.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["checks"], {"rules": "error", "model": "skipped"})

    def test_valid_model_weather_evidence_blocks_and_is_forced_model_source(self):
        plan, search, basic = fixture()
        _, client = self.model({"passed": False, "issues": [{**model_issue(), "source": "rule"}]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertFalse(result["passed"])
        self.assertEqual(result["issues"][0]["source"], "model")
        self.assertEqual(result["checks"]["model"], "completed")
        self.assertEqual(result["plan_revision"], 3)
        sent = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["rule_review"]["rule_summary"], result["rule_summary"])

    def test_model_cannot_turn_default_transit_or_feasible_drive_into_local_buffer_blocker(self):
        for mode, duration, kind in (("transit", 2708, "交通"), ("drive", 848, "交通"), ("transit", 2708, "时间"), ("drive", 848, "时间"), ("walk", None, "交通")):
            with self.subTest(mode=mode, kind=kind):
                plan, search, basic = fixture()
                plan["blocks"][0]["time"] = "12:00-13:00"
                other = {**copy.deepcopy(plan["blocks"][0]), "id":"v2", "name":"公园", "time":"13:40-14:10"}
                plan["blocks"].append(other)
                search["poi"].append({"name":"公园", "url":other["link"]})
                plan["legs"] = [{"from":"v1", "to":"v2", "mode":mode, "duration_s":duration}]
                issue = {**model_issue(kind), "detail":"间隔不足，需预留进出站时间", "block_ids":["v1","v2"],
                         "evidence":[{"path":"plan.legs[0].duration_s", "value":duration},
                                     {"path":"plan.blocks[0].time", "value":"12:00-13:00"},
                                     {"path":"plan.blocks[1].time", "value":"13:40-14:10"}]}
                self.model({"passed":False, "issues":[issue]})
                result = validate.validate_plan(plan, search=search, basic=basic)
                self.assertTrue(result["passed"])
                self.assertFalse(any(i["severity"] == "high" for i in result["issues"]))
                model = next(i for i in result["issues"] if i["source"] == "model")
                self.assertFalse(model["actionable"])

    def test_local_mode_guard_does_not_erase_operational_closure_evidence(self):
        plan, search, basic = fixture()
        plan["blocks"][0]["time"] = "12:00-13:00"
        other = {**copy.deepcopy(plan["blocks"][0]), "id":"v2", "name":"公园", "time":"13:40-14:10"}
        plan["blocks"].append(other)
        search["poi"].append({"name":"公园", "route_status":"道路封闭"})
        plan["legs"] = [{"from":"v1", "to":"v2", "mode":"drive", "duration_s":848}]
        issue = {**model_issue("交通"), "detail":"公园入口道路已封闭", "block_ids":["v1","v2"],
                 "evidence":[{"path":"search.poi[1].route_status", "value":"道路封闭"}]}
        self.model({"passed":False, "issues":[issue]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertFalse(result["passed"])
        self.assertTrue(any(i["severity"] == "high" and i["source"] == "model" for i in result["issues"]))

    def test_explicit_allergy_can_be_reviewed_using_actual_inputs(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(type="美食", name="测试餐厅", link="https://example.test/meal")
        search["food"] = [{"name": "测试餐厅", "url": "https://example.test/meal", "ingredients": ["花生"]}]
        basic["notes"] = "花生过敏，不能食用花生"
        issue = model_issue("偏好", [{"path": "basic.notes", "value": basic["notes"]},
                                     {"path": "search.food[0].ingredients[0]", "value": "花生"}])
        self.model({"passed": False, "issues": [issue]})
        self.assertEqual(validate.validate_plan(plan, search=search, basic=basic)["status"], "blocked")

    def test_unverified_model_high_becomes_warning_not_automatic_repair(self):
        for ev in ([], [{"path": "search.weather.days[99].weather", "value": "暴雨"}],
                   [{"path": "search.weather.days[0].weather", "value": "晴"}],
                   [{"path": "plan.blocks[0].day", "value": True}],
                   [{"path": "search.weather.days[0].weather", "value": "暴雨"},
                    {"path": "basic.unknown", "value": 1}]):
            with self.subTest(evidence=ev):
                plan, search, basic = fixture()
                with patch("validate.OpenAI") as factory:
                    factory.return_value.chat.completions.create.return_value = SimpleNamespace(choices=[
                        SimpleNamespace(message=SimpleNamespace(content=json.dumps({"passed": False, "issues": [model_issue(evidence=ev)]})))])
                    result = validate.validate_plan(plan, search=search, basic=basic)
                self.assertTrue(result["passed"])
                self.assertEqual(result["status"], "warning")
                self.assertEqual(result["issues"][0]["severity"], "medium")
                self.assertFalse(result["issues"][0]["actionable"])
                self.assertIsNone(repair_feedback(result))

    def test_unknown_quotes_survive_model_pass(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(price_known=False, price=0, unit_price=None)
        self.model({"passed": True, "issues": []})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["rule_summary"]["budget_by_style"]["经典"]["budget_status"], "unknown")
        self.assertTrue(any(i["source"] == "rule" and i["type"] == "预算" for i in result["issues"]))

    def test_medium_only_model_false_is_reconciled_to_warning(self):
        plan, search, basic = fixture()
        self.model({"passed": False, "issues": [{"severity": "medium", "type": "偏好",
                    "detail": "可以放慢节奏", "suggestion": "减少活动", "actionable": True}]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "warning")
        self.assertIsNone(repair_feedback(result))

    def test_soft_meal_allocation_does_not_become_budget_block(self):
        plan, search, basic = fixture()
        basic["meal_budget"] = 10
        self.model({"passed": False, "issues": [model_issue("预算", [
            {"path": "basic.meal_budget", "value": 10}, {"path": "plan.blocks[0].price", "value": 30}])]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "warning")
        self.assertIn("规则核算", result["issues"][0]["detail"])

    def test_explicit_specialty_cap_can_support_model_budget_block(self):
        plan, search, basic = fixture()
        basic["hard_limits"] = {"ticket_price": 20}
        self.model({"passed": False, "issues": [model_issue("预算", [
            {"path": "basic.hard_limits.ticket_price", "value": 20}, {"path": "plan.blocks[0].price", "value": 30}])]})
        self.assertEqual(validate.validate_plan(plan, search=search, basic=basic)["status"], "blocked")

    def test_history_only_and_missing_forecast_are_not_hard_requirements(self):
        plan, search, basic = fixture()
        for kind, evidence, extras in (
            ("偏好", [{"path": "recent_trips[0].feedback", "value": "喜欢自然风景"}],
             {"recent_trips": [{"feedback": "喜欢自然风景"}]}),
            ("天气", [{"path": "plan.blocks[0].name", "value": "博物馆"}], {})):
            with self.subTest(kind=kind), patch("validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=json.dumps({"passed": False, "issues": [model_issue(kind, evidence)]})))])
                result = validate.validate_plan(plan, search=search, basic=basic, **extras)
                self.assertEqual(result["status"], "warning")

    def test_unknown_quote_cannot_violate_specialty_cap_or_completeness(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(price=0, unit_price=None, price_known=False)
        basic["hard_limits"] = {"ticket_price": 20}
        evidence = [{"path": "basic.hard_limits.ticket_price", "value": 20},
                    {"path": "plan.blocks[0].price_known", "value": False},
                    {"path": "plan.blocks[0].unit_price", "value": None}]
        for kind in ("预算", "完整性"):
            issue = model_issue(kind, evidence)
            issue["detail"] = "景点暂无报价，不能确认消费，所以无法通过"
            with self.subTest(kind=kind), patch("validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=json.dumps({"passed": False, "issues": [issue]})))])
                result = validate.validate_plan(plan, search=search, basic=basic)
            self.assertEqual(result["status"], "warning")
            self.assertTrue(result["passed"])
            self.assertTrue(all(i["severity"] != "high" for i in result["issues"]))
            self.assertFalse(next(i for i in result["issues"] if i["source"] == "model")["actionable"])
            self.assertIsNone(repair_feedback(result))

    def test_unknown_quote_does_not_erase_actual_weather_conflict(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(price_known=False)
        issue = model_issue("天气", [{"path": "plan.blocks[0].price_known", "value": False},
                                     {"path": "search.weather.days[0].weather", "value": "暴雨"}])
        issue["detail"] = "景点暂无报价，但当天暴雨不适合露天活动"
        self.model({"passed": False, "issues": [issue]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(i["severity"] == "high" and i["type"] == "天气" for i in result["issues"]))

    def test_specialty_cap_with_known_charge_below_cap_cannot_block(self):
        plan, search, basic = fixture()
        basic["hard_limits"] = {"ticket_price": 40}
        self.model({"passed": False, "issues": [model_issue("预算", [
            {"path": "basic.hard_limits.ticket_price", "value": 40},
            {"path": "plan.blocks[0].price", "value": 30}])]})
        self.assertEqual(validate.validate_plan(plan, search=search, basic=basic)["status"], "warning")

    def test_model_failure_keeps_rule_warning_summary_and_history(self):
        plan, search, basic = fixture()
        plan["blocks"][0]["price_known"] = False
        with patch("validate.OpenAI") as factory:
            factory.return_value.chat.completions.create.side_effect = TimeoutError()
            result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["passed"])
        self.assertTrue(result["issues"])
        self.assertEqual(result["checks"]["model"], "error")
        normalized = normalize_audit(result, plan, source="mixed")
        self.assertEqual(normalized, result)
        self.assertEqual(history_entry(1, result)["rule_summary"], result["rule_summary"])
        self.assertIsNone(repair_feedback(result))

    def test_rules_pass_alone_cannot_claim_complete_success(self):
        plan, search, basic = fixture()
        result = combine_audits(validate_rules(plan, search, basic), plan=plan)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["checks"]["model"], "skipped")

    def test_exact_duplicate_keeps_rule_origin(self):
        plan, search, basic = fixture()
        plan["blocks"][0]["price_known"] = False
        rule = validate_rules(plan, search, basic)
        result = combine_audits(rule, {"passed": True, "issues": copy.deepcopy(rule["issues"])}, plan)
        self.assertEqual(result["issues"], rule["issues"])
        self.assertTrue(result["passed"])

    def test_selected_restaurant_zero_index_is_a_real_selected_place(self):
        for selected in (0, 1):
            plan, search, basic = fixture()
            names = ["餐厅甲", "餐厅乙"]
            plan["blocks"][0].update(type="美食", name=names[selected], selected_option=selected,
                                     options=[{"name": n} for n in names], link="https://example.test/meal")
            search["food"] = [{"name": n, "url": "https://example.test/meal"} for n in names]
            with self.subTest(selected=selected):
                result = validate_rules(plan, search, basic)
                self.assertTrue(result["passed"])
                self.assertEqual(result["rule_summary"]["grounding"]["matched_count"], 1)

    def test_context_preserves_indices_aliases_last_train_and_meal_options(self):
        plan, search, _ = fixture()
        plan["blocks"][0]["options"] = [{"name": "另一家"}]
        plan["legs"] = [{"duration_s": 500, "polyline": [1, 2]}]
        search["trains"] = {"outbound": [{"train_no": "G" + str(i)} for i in range(10)]}
        search["poi"].insert(0, {"name": "未选", "aliases": ["来源别名"]})
        original = copy.deepcopy((plan, search))
        sent_plan, sent_search = plan_for_model(plan), search_for_model(search)
        self.assertEqual(sent_search["poi"], search["poi"])
        self.assertEqual(sent_search["trains"], search["trains"])
        self.assertEqual(sent_plan["blocks"][0]["options"], plan["blocks"][0]["options"])
        self.assertNotIn("polyline", sent_plan["legs"][0])
        self.assertEqual((plan, search), original)

    def test_paths_reject_code_trailing_garbage_and_negative_indices(self):
        context = {"plan": {"cost_by_style": {"轻松路线": 30}, "blocks": [{"day": 1}]}}
        self.assertEqual(read_path(context, 'plan.cost_by_style["轻松路线"]'), 30)
        for path in ("plan.blocks[-1].day", "plan.blocks[0].day junk", "plan..blocks",
                     "plan.__class__", "plan.blocks[0].day()", "plan.blocks[0]day", "[0]", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_path(context, path)
        self.assertFalse(same_value(True, 1))
        self.assertFalse(same_value({"day": True}, {"day": 1}))

    def test_standalone_actual_rules_then_model_gets_one_repair(self):
        first, search, basic = fixture(120)
        second, _, _ = fixture(30)
        _, client = self.model({"passed": True, "issues": []})
        with patch.object(run, "generate_plan", side_effect=[first, second]) as generate:
            result = run.run_loop({"search": search, "basic": basic})
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual([e["status"] for e in result["history"]], ["blocked", "passed"])
        self.assertEqual(result["history"][0]["checks"]["model"], "skipped")
        self.assertEqual(result["history"][0]["issues"][0]["source"], "rule")

    def test_supplemental_sources_are_carried_not_invented_from_plan_blocks(self):
        source = {"hotels": [{"name": "原有酒店"}]}
        extra = {"name": "补搜酒店", "url": "https://example.test/hotel"}
        plan = {"source_updates": {"hotels": [extra]}, "blocks": [{"name": "无来源酒店"}]}
        original = copy.deepcopy(source)
        merged = merge_plan_sources(source, plan)
        self.assertEqual(merged["hotels"], [source["hotels"][0], extra])
        self.assertEqual(merge_plan_sources(merged, plan), merged)
        self.assertEqual(source, original)

    def test_unexplained_model_failure_is_incomplete_review(self):
        plan, search, basic = fixture()
        _, client = self.model({"passed": False, "issues": []})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "error")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertIsNone(repair_feedback(result))

    def test_model_cannot_forge_completed_checks_metrics_or_revision(self):
        plan, search, basic = fixture()
        self.model({"passed": True, "issues": [], "plan_revision": 99,
                    "checks": {"rules": "error", "model": "skipped"}, "rule_summary": {"fake": True}})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["checks"], {"rules": "completed", "model": "completed"})
        self.assertEqual(result["plan_revision"], 3)
        self.assertNotIn("fake", result["rule_summary"])

    def test_unselected_meal_options_are_preserved_until_both_checks(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(type="美食", name="午餐（2家可选）",
                                 options=[{"name": "餐厅甲"}, {"name": "餐厅乙"}])
        search["food"] = [{"name": "餐厅甲"}, {"name": "餐厅乙"}]
        _, client = self.model({"passed": True, "issues": []})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["rule_summary"]["grounding"]["named_count"], 0)
        sent = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["plan"]["blocks"][0]["options"], plan["blocks"][0]["options"])

    def test_quoted_field_paths_support_verified_weather_and_budget_facts(self):
        plan, search, basic = fixture()
        basic["hard_limits"] = {"ticket_price": 20}
        for kind, evidence in (
            ("天气", [{"path": 'search["weather"]["days"][0]["weather"]', "value": "暴雨"}]),
            ("预算", [{"path": 'basic["hard_limits"]["ticket_price"]', "value": 20},
                      {"path": 'plan["blocks"][0]["price"]', "value": 30}])):
            with self.subTest(kind=kind), patch("validate.OpenAI") as factory:
                factory.return_value.chat.completions.create.return_value = SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=json.dumps({"passed": False, "issues": [model_issue(kind, evidence)]})))])
                self.assertEqual(validate.validate_plan(plan, search=search, basic=basic)["status"], "blocked")


    def test_weather_object_evidence_expands_and_still_blocks_in_hybrid_review(self):
        plan, search, basic = fixture()
        self.model({"passed": False, "issues": [model_issue(evidence=["search.weather.days[0]"])]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["model"], "completed")
        self.assertTrue(any(e["path"] == "search.weather.days[0].weather" and e["value"] == "暴雨"
                            for e in result["issues"][0]["evidence"]))

    def test_compact_weather_evidence_still_blocks_with_real_input_value(self):
        plan, search, basic = fixture()
        self.model({"passed": False, "issues": [model_issue(evidence=["search.weather.days[0].weather"])]})
        result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["issues"][0]["evidence"], [
            {"path": "search.weather.days[0].weather", "value": "暴雨"}])
        self.assertIn("暴雨", repair_feedback(result))

    def test_truncation_preserves_price_warning_without_passing_or_repairing(self):
        plan, search, basic = fixture()
        plan["blocks"][0].update(price_known=False, unit_price=None, price=0)
        with patch("validate.OpenAI") as factory:
            client = factory.return_value
            client.chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"passed":'))])
            result = validate.validate_plan(plan, search=search, basic=basic)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["checks"], {"rules": "completed", "model": "error"})
        self.assertTrue(any(i["source"] == "rule" and i["type"] == "预算" for i in result["issues"]))
        self.assertIsNone(repair_feedback(result))
        self.assertEqual(client.chat.completions.create.call_count, 2)
