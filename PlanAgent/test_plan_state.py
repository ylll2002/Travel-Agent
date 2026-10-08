import copy
import io
import json
import os
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from unittest.mock import patch

from plan_state import merge_block_edits, rebuild_itineraries


class PlanStateTests(unittest.TestCase):
    def test_rebuild_preserves_stable_ids_meal_options_and_style_summaries(self):
        blocks = [
            {"id": "meal", "plan_style": "经典", "day": 1, "time": "12:00-13:00", "type": "美食", "name": "餐厅乙", "link": "https://example.test/b", "lng": 120.12, "lat": 30.22, "price": 80, "selected_option": 1, "user_selected": True, "options": [{"name": "餐厅甲", "price": 60}, {"name": "餐厅乙", "price": 80, "link": "https://example.test/b", "lng": 120.12, "lat": 30.22}]},
            {"id": "spot", "plan_style": "经典", "day": 1, "time": "09:00-11:00", "type": "景点", "name": "景点"},
            {"id": "other", "plan_style": "轻松", "day": 2, "time": "10:00-11:00", "type": "景点", "name": "其他景点"},
        ]
        original = copy.deepcopy(blocks)
        result = rebuild_itineraries({"destination": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-21", "blocks": blocks, "summaries": {"经典": "文化路线", "轻松": "悠闲路线"}})
        self.assertEqual(blocks, original)
        self.assertEqual([p["summary"] for p in result["plans"]], ["文化路线", "悠闲路线"])
        schedule = result["plans"][0]["itinerary"][0]["schedule"]
        self.assertEqual([item["id"] for item in schedule], ["spot", "meal"])
        self.assertEqual(schedule[1]["options"], original[0]["options"])
        self.assertEqual(schedule[1]["selected_option"], 1)
        self.assertEqual(schedule[1]["date"], "2026-10-20")
        self.assertEqual(result["plans"][1]["itinerary"][0]["date"], "2026-10-21")

    def test_new_place_does_not_inherit_previous_map_coordinates_or_price(self):
        original = {"id": "a1", "name": "旧地点", "day": 1, "plan_style": "经典", "type": "景点", "time": "09:00-11:00", "link": "https://example.test/old", "lng": 120.1, "lat": 30.2, "price": 40, "unit_price": 40, "price_known": True, "price_basis": "per_person", "poi_id": "old"}
        result = merge_block_edits([original], [{"id": "a1", "name": "新地点", "note": "换成这里"}, {"id": "unselected", "name": "模型越界修改"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], original["id"])
        self.assertEqual(result[0]["time"], original["time"])
        for field in ("link", "lng", "lat", "poi_id", "price", "unit_price", "price_basis"):
            self.assertNotIn(field, result[0])
        self.assertEqual(original["name"], "旧地点")

    def test_same_place_text_edit_retains_location_and_explicit_food_choice(self):
        original = {"id": "a1", "name": "餐厅", "link": "https://example.test/food", "lng": 120.1, "lat": 30.2, "price": 80, "options": [{"name": "餐厅", "price": 80}], "user_selected": True}
        result = merge_block_edits([original], [{"id": "a1", "note": "不吃辣"}])
        self.assertEqual(result[0], {**original, "note": "不吃辣"})

    def test_empty_last_day_preserves_original_trip_length(self):
        result = rebuild_itineraries({"start_date": "2026-10-20", "end_date": "2026-10-22", "blocks": [{"id": "a1", "day": 1, "name": "景点"}]})
        self.assertEqual(result["days"], 3)

    def test_day_window_survives_rebuilding_and_deleted_hotel_stays_deleted(self):
        window = {"start_min": 600, "end_min": 1020}
        result = rebuild_itineraries({"start_date": "2026-10-20", "end_date": "2026-10-20",
            "plans": [{"style": "经典", "itinerary": [{"day": 1, "hotel": "已删除酒店"}]}],
            "blocks": [{"id": "s1", "plan_style": "经典", "day": 1, "type": "景点", "name": "景点", "activity_window": window}]})
        day = result["plans"][0]["itinerary"][0]
        self.assertEqual(day["activity_window"], window)
        self.assertNotIn("hotel", day)


class FinalizeContractTests(unittest.TestCase):
    def test_verified_free_attraction_and_masked_transport_price_are_distinguished(self):
        import plan
        result = plan._attach_prices({"blocks": [
            {"id": "s1", "name": "免费景点", "type": "景点"},
            {"id": "t1", "name": "动车D3131", "type": "交通"},
        ]}, {"poi": [{"name": "免费景点", "free": True}],
              "trains": [{"transport": "动车", "train_no": "D3131", "price": "6x"}]}, 4000, {"travelers": "2人"})
        self.assertTrue(result["blocks"][0]["price_known"])
        self.assertEqual(result["blocks"][0]["price"], 0)
        self.assertFalse(result["blocks"][1]["price_known"])
        self.assertEqual(result["unpriced_items"], {"推荐方案": ["动车D3131"]})

    def test_global_cli_can_defer_finalization_to_backend(self):
        import plan

        result = {"destination": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-21", "plans": [{"style": "经典", "itinerary": [{"day": 1, "date": "2026-10-20", "schedule": [{"id": "s1", "type": "景点", "name": "景点", "time": "09:00-11:00"}]}]}]}
        payload = {"plan": result, "modify": {"mode": "global", "instruction": "节奏轻松一些"}, "defer_finalize": True}
        output = io.StringIO()
        with patch.dict("plan.os.environ", {"OPENAI_API_KEY": "local-test-only"}), patch("plan.sys.stdin", io.StringIO(json.dumps(payload))), patch("plan.modify_plan", return_value=result), patch("plan.finalize_plan") as finalize, redirect_stdout(output):
            plan.main()
        finalize.assert_not_called()
        response = json.loads(output.getvalue())
        self.assertEqual(response["blocks"][0]["id"], "s1")
        self.assertEqual(response["blocks"][0]["name"], "景点")

    def test_costs_are_per_style_and_people_and_are_idempotent(self):
        import plan

        sample = {"blocks": [
            {"id": "s1", "plan_style": "经典", "type": "景点", "name": "门票", "price": 10},
            {"id": "m1", "plan_style": "经典", "type": "美食", "name": "餐厅", "price": 80, "user_selected": True},
            {"id": "h1", "plan_style": "经典", "type": "酒店", "name": "酒店", "price": 300},
            {"id": "s2", "plan_style": "轻松", "type": "景点", "name": "另个景点", "price": 200},
        ]}
        basic = {"travelers": "2人", "total_budget": 450}
        result = plan._attach_prices(sample, {"food": [{"name": "餐厅", "price_per_person": 999}]}, 450, basic)
        self.assertEqual(result["cost_by_style"], {"经典": 480, "轻松": 400})
        self.assertEqual(result["total_cost"], 480)
        self.assertEqual(result["budget_by_style"], {"经典": "over", "轻松": "ok"})
        self.assertEqual(result["blocks"][1]["unit_price"], 80)
        self.assertEqual(result["blocks"][2]["price"], 300)
        result = plan._attach_prices(result, {}, 450, basic)
        self.assertEqual(result["cost_by_style"], {"经典": 480, "轻松": 400})

    def test_free_entry_and_unknown_prices_have_different_budget_status(self):
        import plan

        result = plan._attach_prices({"blocks": [
            {"id": "free", "plan_style": "免费", "name": "免费公园", "type": "景点", "price": 0},
            {"id": "unknown", "plan_style": "未报价", "name": "未报价餐厅", "type": "美食"},
        ]}, {}, 1000, {"travelers": "2人"})
        self.assertTrue(result["blocks"][0]["price_known"])
        self.assertFalse(result["blocks"][1]["price_known"])
        self.assertEqual(result["budget_by_style"], {"免费": "ok", "未报价": "unknown"})
        self.assertEqual(result["unpriced_items"], {"未报价": ["未报价餐厅"]})

    def test_explicit_food_price_and_coordinates_survive_no_model_finalize(self):
        import plan

        selected = {"id": "m1", "plan_style": "经典", "day": 1, "date": "2026-10-20", "time": "12:00-13:00", "type": "美食", "name": "餐厅乙", "price": 90, "lng": 120.12, "lat": 30.22, "link": "https://example.test/b", "selected_option": 1, "options": [{"name": "餐厅甲", "price": 60}, {"name": "餐厅乙", "price": 90, "lng": 120.12, "lat": 30.22, "link": "https://example.test/b"}], "user_selected": True}
        original = {"destination": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-21", "blocks": [selected], "summaries": {"经典": "文化路线"}}
        original_copy = copy.deepcopy(original)
        with patch("plan.OpenAI") as model, patch("plan.subprocess.run") as search, patch("plan.attach_routes", side_effect=lambda result, city: result):
            result = plan.finalize_plan(original, {}, {"travelers": "1人", "total_budget": 1000})
        model.assert_not_called()
        search.assert_not_called()
        self.assertEqual(original, original_copy)
        block = result["blocks"][0]
        self.assertEqual(block["price"], 90)
        self.assertEqual(block["link"], "https://example.test/b")
        self.assertEqual(block["lng"], 120.12)
        self.assertEqual(result["plans"][0]["itinerary"][0]["schedule"][0]["id"], "m1")

    def test_map_uses_explicit_option_index_when_block_coordinates_are_missing(self):
        import route_map

        block = {"id": "m1", "type": "美食", "name": "餐厅乙", "selected_option": 1, "options": [{"name": "餐厅甲", "lng": 120.11, "lat": 30.21}, {"name": "餐厅乙", "lng": 120.12, "lat": 30.22}]}
        with patch.dict(os.environ, {"AMAP_KEY": "local-test-only"}), patch("route_map.geocode", return_value={"location": "120.1,30.2", "citycode": "0571"}), patch("route_map._save_cache"):
            route_map.geocode_blocks([block], "杭州")
        self.assertEqual(block["lng"], 120.12)
        self.assertEqual(block["lat"], 30.22)
        self.assertEqual(block["_geo"]["location"], "120.12,30.22")


class MealRevisionTests(unittest.TestCase):
    def setUp(self):
        walking = patch("plan.walking_route", return_value=None)
        walking.start()
        self.addCleanup(walking.stop)

    @staticmethod
    def restaurant(name, lng, price):
        return {"name": name, "longitude": lng, "latitude": 30.27, "price_per_person": price, "rating": 4.5, "poi_detail_url": f"https://example.test/{name}"}

    def test_model_suggestion_from_another_day_is_replaced_with_nearby_cheaper_food(self):
        import plan

        spot = {"id": "s1", "day": 1, "plan_style": "经典", "type": "景点", "time": "09:00-11:00", "name": "午餐前景点", "lng": 120.15, "lat": 30.27}
        meal = {"id": "m1", "day": 1, "plan_style": "经典", "type": "美食", "time": "12:00-13:00", "meal": "午餐", "name": "原餐厅", "price": 200, "unit_price": 100, "price_basis": "per_person", "price_known": True, "lng": 120.153, "lat": 30.27, "link": "https://example.test/old", "options": [{"name": "原餐厅", "price": 100, "lng": 120.153, "lat": 30.27, "link": "https://example.test/old"}]}
        near = self.restaurant("附近实惠店", 120.151, 40)
        far = {**self.restaurant("另一日远店", 120.8, 20), "day": 2, "plan_style": "经典", "meal": "午餐"}
        walk_too_far = self.restaurant("超出步行范围店", 120.17, 30)
        search = {"destination": "杭州", "food": [near, far, walk_too_far], "food_by_anchor": [{"day": 1, "plan_style": "经典", "meal": "午餐", "time": meal["time"], "anchor_name": spot["name"], "longitude": spot["lng"], "latitude": spot["lat"], "restaurants": [near, walk_too_far]}]}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"blocks": [{**meal, "name": far["name"]}]})))])
        with patch("plan.OpenAI"), patch("plan._chat_completion", return_value=response) as model:
            result = plan.modify_blocks([meal], "换一家便宜一些的餐厅，不要太远，步行就能到", search, basic={"travelers": "2人", "total_budget": 2000, "duration_days": 2}, full_blocks=[spot, meal])
        prompt = json.loads(model.call_args.kwargs["messages"][1]["content"])
        self.assertEqual([item["name"] for item in prompt["search"]["food"]], [near["name"]])
        selected = result["blocks"][0]
        self.assertEqual(selected["id"], meal["id"])
        self.assertEqual(selected["time"], meal["time"])
        self.assertEqual(selected["name"], near["name"])
        self.assertEqual(selected["selected_option"], near["name"])
        self.assertLessEqual(selected["distance_m"], 1500)
        self.assertEqual(selected["price"], 40)
        self.assertEqual(selected["link"], near["poi_detail_url"])
        self.assertEqual(selected["lng"], near["longitude"])
        self.assertNotIn("unit_price", selected)
        self.assertEqual(meal["name"], "原餐厅")

    def test_missing_attraction_anchor_retains_original_food_and_explains(self):
        import plan

        meal = {"id": "m1", "day": 1, "plan_style": "经典", "type": "美食", "time": "12:00-13:00", "meal": "午餐", "name": "原餐厅", "price": 100, "lng": 120.153, "lat": 30.27, "link": "https://example.test/old"}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"blocks": [{"id": "m1", "name": "模型编造的店"}]})))])
        with patch("plan.OpenAI"), patch("plan._chat_completion", return_value=response):
            result = plan.modify_blocks([meal], "餐厅便宜一些", {"destination": "杭州", "food": [self.restaurant("远处店", 121, 20)]}, full_blocks=[meal])
        self.assertEqual(result["blocks"][0]["name"], meal["name"])
        self.assertEqual(result["blocks"][0]["link"], meal["link"])
        self.assertIn("保留原推荐", result["blocks"][0]["note"])

    def test_place_rename_cannot_keep_old_coordinate_or_unit_price(self):
        import plan

        original = {"id": "s1", "day": 1, "plan_style": "经典", "type": "景点", "time": "09:00-11:00", "name": "旧景点", "price": 40, "unit_price": 40, "price_known": True, "price_basis": "per_person", "lng": 120.1, "lat": 30.2, "link": "https://example.test/old"}
        destination = {"name": "新景点", "longitude": 120.2, "latitude": 30.3, "price": 90, "url": "https://example.test/new"}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"blocks": [{**original, "name": "新景点"}]})))])
        with patch("plan.OpenAI"), patch("plan._chat_completion", return_value=response):
            result = plan.modify_blocks([original], "换成新景点", {"destination": "杭州", "poi": [destination]}, full_blocks=[original])
        changed = result["blocks"][0]
        self.assertEqual(changed["lng"], destination["longitude"])
        self.assertEqual(changed["lat"], destination["latitude"])
        self.assertEqual(changed["link"], destination["url"])
        self.assertEqual(changed["price"], 90)
        self.assertNotIn("unit_price", changed)
        priced = plan._attach_prices(result, {}, 1000, {"travelers": "2人"})
        self.assertEqual(priced["blocks"][0]["unit_price"], 90)
        self.assertEqual(priced["blocks"][0]["price"], 180)


if __name__ == "__main__":
    unittest.main()
