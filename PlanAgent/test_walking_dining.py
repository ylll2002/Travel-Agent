"""餐厅距离要计入可核实的步行绕行；所有地图接口都用 stub。"""
import copy
import unittest
from unittest.mock import patch

import plan
import route_map


def restaurant(name, lng=120.151, walking=500, price=60):
    return {"name": name, "longitude": lng, "latitude": 30.27,
        "price_per_person": price, "rating": 4.5,
        "walking_origin": "120.150000,30.270000",
        "walking_distance_m": walking, "walking_duration_s": 600}


class WalkingDiningTests(unittest.TestCase):
    def test_nearby_drink_shop_cannot_replace_a_real_meal(self):
        tea = {**restaurant("奶茶", lng=120.1501, price=15), "cuisine": "冷饮店"}
        food = {**restaurant("正餐", lng=120.152, price=50), "cuisine": "中餐厅"}
        choices = plan._pick_restaurants([tea, food], "", set(), [120.15, 30.27], None)
        self.assertEqual([item["name"] for item in choices], ["正餐"])

    def test_short_crow_distance_cannot_hide_five_kilometer_detour(self):
        choices = plan._pick_restaurants([restaurant("湖对面", walking=5748),
            restaurant("同一岸", lng=120.155, walking=700)], "", set(), [120.15, 30.27], None)
        self.assertEqual([item["name"] for item in choices], ["同一岸"])
        self.assertEqual(choices[0]["walking_distance_m"], 700)

    def test_actual_walk_distance_reduces_rank_even_below_hard_cutoff(self):
        choices = plan._pick_restaurants([restaurant("围墙外", walking=3000),
            restaurant("街角", lng=120.153, walking=500)], "", set(), [120.15, 30.27], None)
        self.assertEqual(choices[0]["name"], "街角")
        self.assertLess(choices[1]["distance_m"], choices[0]["distance_m"])

    def test_other_anchor_walk_distance_is_not_reused(self):
        source = restaurant("附近", walking=5748)
        source["walking_origin"] = "121.000000,31.000000"
        choices = plan._pick_restaurants([source], "", set(), [120.15, 30.27], None)
        self.assertEqual(len(choices), 1)
        self.assertIsNone(choices[0]["walking_distance_m"])
        self.assertIsNone(choices[0]["walking_duration_s"])

    def test_small_repeat_penalty_preserves_distance_priority(self):
        original = restaurant("昨天吃过", walking=500)
        alternative = restaurant("另一近店", lng=120.152, walking=550)
        choices = plan._pick_restaurants([original, alternative], "", {original["name"]}, [120.15, 30.27], None)
        self.assertEqual(choices[0]["name"], alternative["name"])
        remote = restaurant("远店", lng=120.18, walking=3500)
        choices = plan._pick_restaurants([original, remote], "", {original["name"]}, [120.15, 30.27], None)
        self.assertEqual(choices[0]["name"], original["name"])
        self.assertEqual(len(plan._pick_restaurants([original], "", {original["name"]}, [120.15, 30.27], None)), 1)

    def test_consecutive_meals_prefer_another_good_restaurant_within_one_kilometer(self):
        original = restaurant("午餐吃过", walking=338, price=98)
        other = restaurant("另一好店", lng=120.155, walking=751, price=60)
        choices = plan._pick_restaurants([original, other], "", {original["name"]}, [120.15, 30.27], None, meal_budget=83)
        self.assertEqual(choices[0]["name"], other["name"])

    def test_enrichment_is_bounded_and_deduplicates_identical_anchors(self):
        entry = {"longitude": 120.15, "latitude": 30.27,
            "restaurants": [restaurant(str(i), lng=120.151 + i * 0.001, walking=None) for i in range(12)]}
        entries = [entry, copy.deepcopy(entry)]
        with patch("plan.walking_route", return_value={"mode": "walk", "distance_m": 800, "duration_s": 620}) as walking:
            plan._enrich_food_walking(entries)
        self.assertEqual(walking.call_count, 8)
        self.assertEqual(len(entries[0]["restaurants"]), 8)
        self.assertTrue(all(item["walking_distance_m"] == 800 for entry in entries for item in entry["restaurants"]))

    def test_failed_route_lookup_keeps_straight_distance_without_inventing_walk(self):
        entries = [{"longitude": 120.15, "latitude": 30.27,
            "restaurants": [restaurant("附近")]}]
        with patch("plan.walking_route", return_value=None):
            plan._enrich_food_walking(entries)
        source = entries[0]["restaurants"][0]
        self.assertIsNone(source["walking_distance_m"])
        self.assertNotIn("步行", plan._meal_distance_note("午餐", "景点", source))

    def test_walking_requirement_excludes_route_over_1500_meters(self):
        spot = {"id": "s1", "name": "景点", "type": "景点", "day": 1, "plan_style": "经典",
            "time": "09:00-11:00", "lng": 120.15, "lat": 30.27}
        meal = {"id": "m1", "name": "原店", "type": "美食", "day": 1, "plan_style": "经典",
            "meal": "午餐", "time": "12:00-13:00", "unit_price": 100}
        a, b = restaurant("绕路店", walking=None), restaurant("近店", lng=120.152, walking=None)
        def walking(origin, dest):
            meters = 2200 if dest.startswith("120.151000") else 700
            return {"distance_m": meters, "duration_s": meters}
        with patch("plan.walking_route", side_effect=walking):
            choices, _ = plan._food_choices_for_edit(meal, [spot, meal], {"food": [a, b]}, {}, "换一家，步行能到")
        self.assertEqual([item["name"] for item in choices], ["近店"])

    def test_changed_anchor_clears_old_walking_distance_and_note(self):
        spot = {"id": "s1", "name": "新景点", "type": "景点", "day": 1, "time": "09:00-11:00", "lng": 120.15, "lat": 30.27}
        meal = {"id": "m1", "name": "餐厅", "type": "美食", "day": 1, "time": "12:00-13:00",
            "lng": 120.151, "lat": 30.27, "walking_origin": "121.000000,31.000000",
            "walking_distance_m": 1200, "walking_duration_s": 900,
            "note": "午餐 · 距旧景点约10米（直线距离）；步行约1200米/15分钟；用户保留备注"}
        with patch("plan.walking_route", return_value=None):
            plan._refresh_selected_meal_distances({"blocks": [spot, meal]})
        self.assertIsNone(meal["walking_distance_m"])
        self.assertNotIn("步行", meal["note"])
        self.assertIn("新景点", meal["note"])
        self.assertIn("用户保留备注", meal["note"])

    def test_new_restaurant_same_anchor_cannot_inherit_previous_walk(self):
        spot = {"name": "景点", "type": "景点", "day": 1, "time": "09:00-11:00", "lng": 120.15, "lat": 30.27}
        meal = {"name": "新餐厅", "type": "美食", "day": 1, "time": "12:00-13:00", "lng": 120.152, "lat": 30.27,
            "walking_origin": "120.150000,30.270000", "walking_distance_m": 100, "walking_duration_s": 90}
        with patch("plan.walking_route", return_value=None):
            plan._refresh_selected_meal_distances({"blocks": [spot, meal]})
        self.assertIsNone(meal["walking_distance_m"])
        self.assertIsNone(meal["walking_duration_s"])

    def test_deleted_last_attraction_clears_meal_anchor_and_distance(self):
        meal = {"name": "手选餐厅", "type": "美食", "day": 1, "time": "12:00-13:00", "lng": 120.152, "lat": 30.27,
            "anchor_name": "已删除景点", "distance_m": 100, "walking_origin": "120.150000,30.270000",
            "walking_distance_m": 100, "walking_duration_s": 90,
            "note": "午餐 · 距已删除景点约100米（直线距离）；步行约100米/2分钟；手选保留备注"}
        plan._refresh_selected_meal_distances({"blocks": [meal]})
        self.assertNotIn("anchor_name", meal)
        self.assertNotIn("distance_m", meal)
        self.assertNotIn("walking_distance_m", meal)
        self.assertEqual(meal["note"], "手选保留备注")


class WalkingRouteTests(unittest.TestCase):
    def test_walking_lookup_never_relabels_driving_route(self):
        cache = {"leg": {"120.150000,30.270000>120.151000,30.270000": {"mode": "drive", "distance_m": 500}}, "walking": {}}
        with patch("route_map._load_cache", return_value=cache), patch("route_map._walk_or_drive") as query:
            self.assertIsNone(route_map.walking_route("120.15,30.27", "120.151,30.27", cached_only=True))
        query.assert_not_called()

    def test_long_walk_detour_uses_available_transit_on_map(self):
        cache = {"leg": {}, "walking": {}}
        transit = {"mode": "transit", "distance_m": 3000, "duration_s": 900}
        with patch("route_map._load_cache", return_value=cache), \
             patch("route_map.walking_route", return_value={"mode": "walk", "distance_m": 5748, "duration_s": 4000}), \
             patch("route_map._transit", return_value=transit), patch("route_map._walk_or_drive") as drive:
            leg = route_map.route_leg({"location": "120.15,30.27", "citycode": "0571"}, {"location": "120.151,30.27"})
        self.assertEqual(leg["mode"], "transit")
        drive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
