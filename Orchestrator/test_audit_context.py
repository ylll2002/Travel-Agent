import copy
import unittest

from unittest.mock import patch
import orchestrator as flow
from ValidateAgent.context import plan_for_model as _plan_for_validate, search_for_model as _search_for_validate


class AuditContextTests(unittest.TestCase):
    def test_review_keeps_selected_meal_cost_distance_and_route_duration(self):
        meal = {"name": "附近餐厅", "price": 160, "distance_m": 120,
                "options": [{"name": "另一家"}]}
        plan = {"blocks": [meal], "plans": [{"itinerary": [{"schedule": [meal]}]}],
                "food": [meal], "food_by_anchor": [{"restaurants": [meal]}],
                "legs": [{"duration": 120, "polyline": [[120, 30]]}],
                "unpriced_items": {"推荐": ["酒店"]}}
        original = copy.deepcopy(plan)
        result = _plan_for_validate(plan)
        self.assertEqual(result["blocks"][0]["price"], 160)
        self.assertEqual(result["blocks"][0]["distance_m"], 120)
        self.assertEqual(result["legs"], [{"duration": 120}])
        self.assertEqual(result["unpriced_items"], {"推荐": ["酒店"]})
        self.assertEqual(result["blocks"][0]["options"], meal["options"])
        self.assertNotIn("food_by_anchor", result)
        self.assertEqual(plan, original)

    def test_review_search_keeps_weather_and_selected_source(self):
        search = {"weather": {"days": [{"weather": "雨"}]},
                  "food": [{"name": "选中餐厅", "cuisine": "杭帮菜", "price_per_person": 80},
                           {"name": "其他餐厅"}], "social_food": [{"text": "长文"}]}
        result = _search_for_validate(search, {"blocks": [{"name": "选中餐厅"}]})
        self.assertEqual(result["weather"], search["weather"])
        self.assertEqual(result["food"], search["food"])
        self.assertNotIn("social_food", result)

    def test_main_flow_preserves_audit_locations_revision_and_history(self):
        plan = {"revision": 7, "blocks": [{"id": "b1", "plan_style": "经典", "day": 1, "date": "2026-10-10"}]}
        issue = {"severity": "high", "type": "时间", "detail": "赶不上列车", "suggestion": "提前结束",
                 "actionable": True, "plan_style": "经典", "day": 1, "date": "2026-10-10", "block_ids": ["b1"],
                 "evidence": [{"path": "plan.legs[0].duration_s", "value": 2700}]}
        state = {"plan": plan, "iteration": 1, "profile": {"city": "福州"},
                 "preferences": {"culture": True}, "recent_trips": [{"destination": "上海"}],
                 "search": {"weather": {"rain": True}}, "basic": {"total_budget": 4000}}
        with patch.object(flow, "_call", return_value={"passed": False, "issues": [issue]}) as call:
            result = flow.validate_node(state)
        payload = call.call_args.args[2]
        for key in ("profile", "preferences", "recent_trips", "basic"):
            self.assertEqual(payload[key], state[key])
        self.assertEqual(payload["search"], state["search"])
        self.assertEqual(payload["plan"], plan)
        self.assertEqual(result["audit"]["plan_revision"], 7)
        self.assertEqual(result["audit"]["status"], "blocked")
        self.assertEqual(result["history"][0]["issues"][0]["block_ids"], ["b1"])
        self.assertEqual(result["history"][0]["issues"][0]["evidence"], issue["evidence"])
        self.assertIn("赶不上列车", result["feedback"])

    def test_invented_location_is_service_error_and_does_not_replan(self):
        plan = {"revision": 1, "blocks": [{"id": "real"}]}
        raw = {"passed": False, "issues": [{"severity": "high", "detail": "冲突", "suggestion": "修改",
                                           "actionable": True, "block_ids": ["invented"]}]}
        with patch.object(flow, "_call", return_value=raw):
            result = flow.validate_node({"plan": plan, "iteration": 1})
        self.assertEqual(result["audit"]["status"], "error")
        self.assertFalse(result["audit"]["passed"])
        self.assertIsNone(result["feedback"])
        self.assertEqual(flow.should_continue({"plan": plan, "iteration": 1, **result}), "end")


if __name__ == "__main__":
    unittest.main()
