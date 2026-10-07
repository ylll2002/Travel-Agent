"""Every edit reviews finalized prices/routes once and drops the old verdict."""
import copy
import unittest
from unittest.mock import patch

from app.api.routes import plan as routes
from shared.audit import combine_audits, failed_audit, normalize_audit
from ValidateAgent.rules import validate_rules


def block(identifier="v1", name="博物馆", day=1, time="09:00-11:00", price=30):
    return {"id": identifier, "plan_style": "经典", "day": day, "date": f"2026-10-{9+day}",
            "type": "景点", "time": time, "name": name, "price": price, "price_known": True,
            "link": "https://example.test/" + name}


class ModificationReviewTests(unittest.TestCase):
    def setUp(self):
        self.old = {"revision": 4, "blocks": [block()], "basic": {"total_budget": 100},
                    "audit": {"passed": True, "issues": []}, "history": [{"passed": True}], "passed": True}
        self.source = {"poi": [{"name": n, "url": "https://example.test/" + n} for n in ("博物馆", "公园")],
                       "weather": {"days": [{"date": "2026-10-10", "weather": "晴"}]}}
        self.calls = []

    def request(self, change, **kwargs):
        return routes.PlanRequest(destination="杭州", start_date="2026-10-10", end_date="2026-10-11",
            plan=copy.deepcopy(self.old), search=copy.deepcopy(self.source),
            profile={"city": "福州"}, preferences={"culture": True}, recent_trips=[{"destination": "上海"}],
            modify={"blocks": copy.deepcopy(self.old["blocks"]), "plan_style": "经典", **change}, **kwargs)

    def runner(self, python, script, data, **kwargs):
        self.calls.append((script, copy.deepcopy(data)))
        if script == routes.PLAN_PY and data.get("finalize"):
            return {**copy.deepcopy(data["plan"]), "legs": [{"from": "v1", "to": "v2", "duration_s": 1800}],
                    "cost_by_style": {"经典": sum(b.get("price", 0) for b in data["plan"]["blocks"])}}
        if script == routes.VALIDATE_PY:
            rule = validate_rules(data["plan"], data["search"], data["basic"])
            return combine_audits(rule, {"passed": True, "issues": []} if rule["passed"] else None, data["plan"])
        raise AssertionError("Unexpected subprocess")

    def execute(self, payload):
        with patch.object(routes, "_run_json", side_effect=self.runner):
            return routes.create_plan(payload)

    def test_add_delete_and_update_all_review_once_after_finalizing(self):
        changes = (
            {"action": "add", "day": 2, "item": {"name": "公园", "type": "景点", "price": 20}},
            {"action": "delete", "block_ids": ["v1"]},
            {"action": "update", "block_ids": ["v1"], "item": {"name": "公园", "type": "景点", "price": 20}},
        )
        for change in changes:
            with self.subTest(action=change["action"]):
                self.calls.clear()
                result = self.execute(self.request(change))
                self.assertEqual([script for script, _ in self.calls], [routes.PLAN_PY, routes.VALIDATE_PY])
                finalizer, review = self.calls[0][1], self.calls[1][1]
                self.assertNotIn("audit", finalizer["plan"])
                self.assertNotIn("history", finalizer["plan"])
                self.assertEqual(result["revision"], 5)
                self.assertEqual(result["audit"]["plan_revision"], 5)
                self.assertEqual(review["plan"]["legs"], result["legs"])
                self.assertEqual(review["plan"]["cost_by_style"], result["cost_by_style"])
                self.assertEqual(len(result["history"]), 1)
                self.assertEqual(result["history"][0]["plan_revision"], 5)
                self.assertEqual(review["preferences"], {"culture": True})
                self.assertEqual(review["recent_trips"], [{"destination": "上海"}])
        self.assertEqual(self.old["revision"], 4)

    def test_explicit_food_choice_is_reviewed_without_changing_it_again(self):
        self.old["blocks"] = [{**block(), "type": "美食", "name": "旧餐厅", "time": "12:00-13:00"}]
        self.source["food"] = [{"name": "餐厅乙", "price_per_person": 120, "url": "https://example.test/food"}]
        result = self.execute(self.request({"action": "update", "block_ids": ["v1"],
            "item": {"name": "餐厅乙", "type": "美食", "price": 120, "link": "https://example.test/food"}}))
        self.assertEqual(result["blocks"][0]["name"], "餐厅乙")
        self.assertEqual(result["audit"]["status"], "blocked")
        self.assertFalse(result["passed"])
        self.assertEqual(len(self.calls), 2, "No automatic rewrite after an explicit choice")

    def test_local_replanning_review_includes_untouched_following_blocks(self):
        self.old["blocks"].append(block("v2", "公园", time="11:10-12:10"))
        local = {"blocks": [{**self.old["blocks"][0], "time": "09:00-11:05"}]}
        with patch.object(routes, "_local_modify", return_value=local):
            result = self.execute(self.request({"block_ids": ["v1"], "instruction": "延长参观"}))
        self.assertEqual(len(self.calls[1][1]["plan"]["blocks"]), 2)
        self.assertEqual(result["audit"]["status"], "blocked")
        self.assertTrue(any(i["type"] == "交通" and set(i["block_ids"]) == {"v1", "v2"} for i in result["audit"]["issues"]))

    def test_global_date_budget_change_reviews_actual_new_context(self):
        changed = {"blocks": [block(price=150)]}
        with patch.object(routes, "_modify_plan", return_value=changed), \
             patch.object(routes, "_search_context", return_value=copy.deepcopy(self.source)):
            result = self.execute(self.request({"mode": "global", "instruction": "改成10月11日至10月12日，预算200元"}))
        review = self.calls[-1][1]
        self.assertEqual(result["start_date"], "2026-10-11")
        self.assertEqual(result["end_date"], "2026-10-12")
        self.assertEqual(review["basic"]["total_budget"], 200)
        self.assertEqual(review["plan"]["blocks"][0]["date"], "2026-10-11")
        self.assertEqual(review["search"]["start_date"], "2026-10-11")
        self.assertNotEqual(result["audit"], self.old["audit"])

    def test_model_error_retains_edit_and_new_rule_warnings_not_old_success(self):
        payload = self.request({"action": "update", "block_ids": ["v1"], "item": {"name": "公园", "type": "景点"}})
        def runner(python, script, data, **kwargs):
            if script == routes.VALIDATE_PY:
                rule = validate_rules(data["plan"], data["search"], data["basic"])
                return combine_audits(rule, failed_audit("模型超时", "稍后重试", data["plan"]), data["plan"])
            return self.runner(python, script, data, **kwargs)
        with patch.object(routes, "_run_json", side_effect=runner):
            result = routes.create_plan(payload)
        self.assertEqual(result["blocks"][0]["name"], "公园")
        self.assertEqual(result["audit"]["status"], "error")
        self.assertEqual(result["audit"]["checks"]["model"], "error")
        self.assertEqual(result["audit"]["plan_revision"], 5)
        self.assertNotIn("error", result, "An audit failure is not a mutation failure")
        self.assertFalse(result["passed"])

    def test_old_revision_or_unavailable_service_cannot_restore_old_pass(self):
        payload = self.request({"action": "update", "block_ids": ["v1"], "item": {"name": "公园", "type": "景点"}})
        for audit in (normalize_audit({"passed": True, "issues": []}, self.old),
                      {"error": "service unavailable"}, {"passed": True, "issues": [], "plan_revision": True}):
            def runner(python, script, data, **kwargs):
                return audit if script == routes.VALIDATE_PY else self.runner(python, script, data, **kwargs)
            with self.subTest(audit=audit), patch.object(routes, "_run_json", side_effect=runner):
                result = routes.create_plan(payload)
                self.assertEqual(result["audit"]["status"], "error")
                self.assertEqual(result["audit"]["plan_revision"], 5)
                self.assertFalse(result["passed"])
                self.assertEqual(result["blocks"][0]["name"], "公园")

    def test_legacy_plan_without_revision_starts_at_one(self):
        self.old.pop("revision")
        result = self.execute(self.request({"action": "delete", "block_ids": ["v1"]}))
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["audit"]["status"], "blocked", "Deleting every block must not pass")

    def test_finalization_failure_never_calls_validator_or_reuses_old_review(self):
        with patch.object(routes, "_run_json", return_value={"error": "route failure"}) as runner:
            result = routes.create_plan(self.request({"action": "delete", "block_ids": ["v1"]}))
        self.assertEqual(result, {"error": "route failure"})
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
