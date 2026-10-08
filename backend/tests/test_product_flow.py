"""Exercise the real PlanAgent CLI finalizer without model or map requests."""

import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.routes.plan import PLAN_PYTHON, PlanRequest, create_plan


BLOCKS = [
    {"id": "s1", "plan_style": "经典", "day": 1, "date": "2026-10-20", "time": "09:00-11:00", "type": "景点", "name": "景点", "lng": 120.1, "lat": 30.2, "price": 20},
    {"id": "m1", "plan_style": "经典", "day": 1, "date": "2026-10-20", "time": "12:00-13:00", "type": "美食", "name": "餐厅甲", "lng": 120.11, "lat": 30.21, "price": 60, "link": "https://example.test/a", "options": [{"name": "餐厅甲", "price": 60, "lng": 120.11, "lat": 30.21, "link": "https://example.test/a"}]},
    {"id": "s2", "plan_style": "轻松", "day": 1, "date": "2026-10-20", "time": "10:00-11:00", "type": "景点", "name": "第二方案景点", "lng": 120.13, "lat": 30.23, "price": 30},
]


@unittest.skipUnless(Path(PLAN_PYTHON).exists(), "PlanAgent local environment is unavailable")
class ProductFlowIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Empty values are deliberate: dotenv must not load a real map key into
        # the subprocess. Finalization does not require a model API call.
        env = {**os.environ, "AMAP_KEY": "", "OPENAI_API_KEY": "local-test-only"}
        env.pop("__PYVENV_LAUNCHER__", None)
        self.env_patch = patch("app.api.routes.plan._subprocess_env", return_value=env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def make_request(self, modification, **kwargs):
        metadata = {"blocks": copy.deepcopy(BLOCKS), "summaries": {"经典": "文化路线", "轻松": "悠闲路线"}, "food": [{"name": "原候选", "price_per_person": 60, "day": 1, "plan_style": "经典"}]}
        metadata.update(kwargs.pop("metadata", {}))
        return PlanRequest(destination="杭州", start_date="2026-10-20", end_date="2026-10-21", basic={"travelers": "1人", "total_budget": 1000}, search={"destination": "杭州", "poi": []}, plan=metadata, modify={"blocks": copy.deepcopy(BLOCKS), **modification}, **kwargs)

    def test_explicit_restaurant_choice_survives_backend_cli_and_nested_plan(self):
        choice = {"type": "美食", "name": "餐厅乙", "price": 90, "lng": 120.12, "lat": 30.22, "link": "https://example.test/b", "options": [{"name": "餐厅甲", "price": 60}, {"name": "餐厅乙", "price": 90, "lng": 120.12, "lat": 30.22, "link": "https://example.test/b"}], "selected_option": 1}
        result = create_plan(self.make_request({"action": "update", "plan_style": "经典", "block_ids": ["m1"], "item": choice}))
        self.assertNotIn("error", result, result.get("error"))
        meal = next(item for item in result["blocks"] if item["id"] == "m1")
        for field in ("name", "price", "link", "lng", "lat"):
            self.assertEqual(meal[field], choice[field])
        self.assertEqual(len(meal["options"]), len(choice["options"]))
        for actual, original in zip(meal["options"], choice["options"]):
            for field, value in original.items():
                self.assertEqual(actual[field], value)
        chosen = next(option for option in meal["options"] if option["name"] == choice["name"])
        self.assertIsInstance(chosen["distance_m"], int)
        self.assertGreater(chosen["distance_m"], 0)
        self.assertLess(chosen["distance_m"], 5000)
        self.assertEqual(chosen["distance_km"], round(chosen["distance_m"] / 1000, 1))
        self.assertEqual(meal["distance_m"], chosen["distance_m"])
        self.assertEqual(meal["anchor_name"], "景点")
        self.assertTrue(meal["user_selected"])
        nested = next(item for item in result["plans"][0]["itinerary"][0]["schedule"] if item["id"] == "m1")
        self.assertEqual(nested["link"], choice["link"])
        self.assertEqual(result["blocks"][-1]["name"], "第二方案景点")
        self.assertEqual(result["plans"][1]["summary"], "悠闲路线")
        self.assertEqual(meal["selected_option"], "餐厅乙")
        self.assertEqual(result["cost_by_style"], {"经典": 110, "轻松": 30})
        self.assertEqual(result["days"], 2)

    def test_changing_existing_candidate_price_clears_old_unit_price(self):
        payload = self.make_request({"action": "update", "plan_style": "经典", "block_ids": ["m1"], "item": {"type": "美食", "name": "餐厅甲", "price": 95, "lng": 120.11, "lat": 30.21, "link": "https://example.test/a"}})
        payload.basic = {"travelers": "2人", "total_budget": 1000}
        meal = next(item for item in payload.modify["blocks"] if item["id"] == "m1")
        meal.update({"price": 120, "unit_price": 60, "price_known": True, "price_basis": "per_person"})
        result = create_plan(payload)
        self.assertNotIn("error", result, result.get("error"))
        selected = next(item for item in result["blocks"] if item["id"] == "m1")
        self.assertEqual(selected["unit_price"], 95)
        self.assertEqual(selected["price"], 190)
        self.assertEqual(result["cost_by_style"], {"经典": 230, "轻松": 60})

    def test_unpriced_new_restaurant_or_hotel_does_not_inherit_old_cost(self):
        for kind, target_id, new_name in (("美食", "m1", "未知报价餐厅"), ("酒店", "h1", "未知报价酒店")):
            for action in ("add", "update"):
                with self.subTest(kind=kind, action=action):
                    payload = self.make_request({"action": action, "plan_style": "经典", "day": 1, "block_ids": [target_id], "item": {"type": kind, "name": new_name, "link": "https://example.test/new", "lng": 120.101, "lat": 30.201}})
                    payload.basic = {"travelers": "2人", "total_budget": 1000}
                    if kind == "酒店":
                        payload.modify["blocks"].append({"id": "h1", "day": 1, "plan_style": "经典", "type": "酒店", "name": "旧酒店", "time": "住宿", "price": 800, "unit_price": 800, "price_basis": "group", "price_known": True})
                    else:
                        old = next(block for block in payload.modify["blocks"] if block["id"] == target_id)
                        old.update(price=120, unit_price=60, price_basis="per_person", price_known=True)
                    result = create_plan(payload)
                    self.assertNotIn("error", result, result.get("error"))
                    changed = next(block for block in result["blocks"] if block["id"] == target_id)
                    self.assertEqual(changed["name"], new_name)
                    self.assertIsNone(changed["unit_price"])
                    self.assertFalse(changed["price_known"])
                    self.assertEqual(changed["price"], 0)
                    self.assertIn(new_name, result["unpriced_items"]["经典"])
                    self.assertEqual(result["budget_by_style"]["经典"], "unknown")

    def test_same_candidate_without_new_quote_keeps_existing_known_unit_price(self):
        for action in ("add", "update"):
            with self.subTest(action=action):
                payload = self.make_request({"action": action, "plan_style": "经典", "day": 1, "block_ids": ["m1"], "item": {"type": "美食", "name": "餐厅甲", "lng": 120.11, "lat": 30.21, "link": "https://example.test/a"}})
                payload.basic = {"travelers": "2人", "total_budget": 1000}
                old = next(block for block in payload.modify["blocks"] if block["id"] == "m1")
                old.update(price=120, unit_price=60, price_basis="per_person", price_known=True)
                result = create_plan(payload)
                changed = next(block for block in result["blocks"] if block["id"] == "m1")
                self.assertEqual(changed["unit_price"], 60)
                self.assertEqual(changed["price"], 120)
                self.assertTrue(changed["price_known"])

    def test_unquoted_new_hotel_uses_its_own_search_quote(self):
        payload = self.make_request({"action": "add", "plan_style": "经典", "day": 1, "block_ids": ["h1"], "item": {"type": "酒店", "name": "新酒店", "link": "https://example.test/hotel"}})
        payload.modify["blocks"].append({"id": "h1", "day": 1, "plan_style": "经典", "type": "酒店", "name": "旧酒店", "time": "住宿", "price": 800, "unit_price": 800, "price_basis": "group", "price_known": True})
        payload.search = {"destination": "杭州", "hotels": [{"name": "新酒店", "price": 350}]}
        result = create_plan(payload)
        changed = next(block for block in result["blocks"] if block["id"] == "h1")
        self.assertEqual(changed["unit_price"], 350)
        self.assertEqual(changed["price"], 350)
        self.assertTrue(changed["price_known"])

    def test_add_restaurant_updates_existing_meal_and_keeps_right_panel_food(self):
        result = create_plan(self.make_request({"action": "add", "plan_style": "经典", "day": 1, "block_ids": ["m1"], "item": {"type": "美食", "name": "餐厅丙", "price": 100, "lng": 120.12, "lat": 30.22, "link": "https://example.test/c"}}))
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(len(result["blocks"]), len(BLOCKS))
        meal = next(item for item in result["blocks"] if item["id"] == "m1")
        self.assertEqual(meal["name"], "餐厅丙")
        self.assertEqual(meal["time"], "12:00-13:00")
        self.assertTrue(meal["user_selected"])
        self.assertEqual(result["food"][0]["name"], "原候选")

    def test_delete_meal_does_not_leave_routes_to_removed_block(self):
        result = create_plan(self.make_request({"action": "delete", "plan_style": "经典", "block_ids": ["m1"]}, metadata={"legs": [{"plan_style": "经典", "day": 1, "from_id": "s1", "to_id": "m1", "polyline": [[120.1, 30.2], [120.11, 30.21]]}]}))
        self.assertNotIn("error", result, result.get("error"))
        self.assertNotIn("m1", [item["id"] for item in result["blocks"]])
        self.assertFalse(any(leg.get("from_id") == "m1" or leg.get("to_id") == "m1" for leg in result.get("legs", [])))
        self.assertEqual([item["id"] for item in result["plans"][0]["itinerary"][0]["schedule"]], ["s1"])

    def test_global_budget_change_reaches_real_finalizer_and_ignores_model_metadata(self):
        payload = self.make_request({"mode": "global", "plan_style": "经典", "instruction": "总预算降到3000元"})
        payload.basic = {"travelers": "2人", "total_budget": 6000, "budget_per_person": 3000, "budget_mode": "per_person"}
        proposed = {"blocks": copy.deepcopy(BLOCKS[:2]), "basic": {"travelers": "99人", "total_budget": 1}, "start_date": "2099-01-01", "end_date": "2099-02-01"}
        with patch("app.api.routes.plan._modify_plan", return_value=proposed) as modify, patch("app.api.routes.plan._search_context") as search:
            result = create_plan(payload)
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(modify.call_args.args[6]["total_budget"], 3000)
        self.assertEqual(modify.call_args.args[6]["travelers"], "2人")
        self.assertNotIn("budget_per_person", result["basic"])
        self.assertEqual(result["basic"]["total_budget"], 3000)
        self.assertEqual(result["basic"]["travelers"], "2人")
        self.assertEqual(result["start_date"], "2026-10-20")
        search.assert_not_called()

    def test_departure_change_refreshes_search_and_aligns_other_style_calendar(self):
        payload = self.make_request({"mode": "global", "plan_style": "经典", "instruction": "改为10月25日出发"})
        proposed = {"blocks": copy.deepcopy(BLOCKS[:2])}
        with patch("app.api.routes.plan._modify_plan", return_value=proposed) as modify, patch("app.api.routes.plan._search_context", return_value={"destination": "杭州", "poi": []}) as search:
            result = create_plan(payload)
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(search.call_args.args[:3], ("杭州", "2026-10-25", "2026-10-26"))
        self.assertEqual(modify.call_args.args[1:3], ("2026-10-25", "2026-10-26"))
        self.assertEqual(result["start_date"], "2026-10-25")
        self.assertEqual(result["end_date"], "2026-10-26")
        self.assertTrue(all(block["date"] == "2026-10-25" for block in result["blocks"]))
        self.assertEqual(next(block for block in result["blocks"] if block["id"] == "s2")["date"], "2026-10-25")

    def test_extended_trip_refresh_includes_added_day(self):
        payload = self.make_request({"mode": "global", "plan_style": "经典", "instruction": "多玩一天"})
        added = {"id": "day3", "plan_style": "经典", "day": 3, "type": "景点", "name": "新增日景点", "time": "09:00-11:00", "lng": 120.15, "lat": 30.25, "price": 20}
        with patch("app.api.routes.plan._modify_plan", return_value={"blocks": [*copy.deepcopy(BLOCKS[:2]), added]}) as modify, patch("app.api.routes.plan._search_context", return_value={"destination": "杭州", "poi": []}), patch("app.api.routes.plan._finalize_blocks", return_value={"blocks": []}) as finalize:
            create_plan(payload)
        self.assertEqual(modify.call_args.args[1:3], ("2026-10-20", "2026-10-22"))
        self.assertEqual(modify.call_args.args[4]["_trip_context"]["days"], 3)
        self.assertEqual(finalize.call_args.args[3], "2026-10-22")
        self.assertIn({"plan_style": "经典", "day": 3}, finalize.call_args.kwargs["refresh_targets"])


if __name__ == "__main__":
    unittest.main()
