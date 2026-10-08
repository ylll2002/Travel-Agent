"""Run with python -m unittest discover -s PlanAgent/tests -v (no API keys)."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replan import ReplanError, replan_plan, replacement, catalog, expected_pairs, parse_selection, selection_context
from shared.travel import travel_metadata, match_travel, attach_travel_metadata


def block(bid, day, kind, name, time, style="推荐方案"):
    return {"id": bid, "day": day, "date": f"2026-10-{day + 19:02d}", "type": kind, "name": name, "time": time, "plan_style": style, "price": 20, "lng": 120.1, "lat": 30.1}


class ReplanTests(unittest.TestCase):
    def setUp(self):
        self.original = {"destination": "杭州", "start_date": "2026-10-20", "end_date": "2026-10-21", "revision": 0, "blocks": [block("a", 1, "景点", "灵隐寺", "09:00-11:00"), block("b", 1, "美食", "午餐店", "11:30-12:30"), block("h", 1, "酒店", "酒店甲", "18:00-18:30"), block("d", 2, "景点", "次日景点", "09:00-11:00"), block("other", 1, "景点", "另一方案", "09:00-11:00", "其他方案")], "legs": [{"plan_style": "其他方案", "day": 1, "from": "x", "to": "y", "duration_s": 10}]}
        self.payload = {"plan": self.original, "revision": 0, "plan_style": "推荐方案", "target_block_ids": ["a"], "instruction": "换个文化景点"}
        self.search = {"poi": [{"name": "博物馆", "price": 30}, {"name": "第二候选", "price": 10}], "hotels": [{"name": "酒店乙", "price": 50}], "food": [{"name": "新餐厅", "price_per_person": 40}]}

    def choose(self, context):
        return {"replacements": [{"block_id": b["id"], "candidate_id": context["candidates"][b["id"]][0]["candidate_id"]} for b in context["targets"]]}

    def routes(self, result, city):
        for i, b in enumerate(result["blocks"]):
            b["lng"], b["lat"] = 120 + i / 100, 30 + i / 100
        result["legs"] = [{"plan_style": "推荐方案", "day": day, "from": left, "to": right, "duration_s": 3600, "mode": "drive", "distance_m": 1000, "polyline": [[120, 30], [120.1, 30.1]]} for day, left, right in expected_pairs(result["blocks"], "推荐方案")]

    def run_plan(self, routes=None, choose=None):
        return replan_plan(self.payload, self.search, choose or self.choose, routes or self.routes)

    def test_replaces_target_and_repairs_downstream_time(self):
        result = self.run_plan()
        by_id = {b["id"]: b for b in result["plan"]["blocks"]}
        self.assertEqual(by_id["a"]["name"], "博物馆")
        self.assertEqual(by_id["b"]["time"], "12:10-13:10")
        self.assertEqual(by_id["a"]["id"], "a")
        self.assertEqual(result["revision"], 1)

    def test_incomplete_model_response_retries_with_feedback(self):
        calls = []
        def choose(context):
            calls.append(context["feedback"])
            return {} if len(calls) == 1 else self.choose(context)
        self.assertEqual(self.run_plan(choose=choose)["revision"], 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("block_id", calls[1])

    def test_invalid_model_shapes_retry_three_times_and_preserve_input(self):
        for bad in (None, [], {"replacements": ["a"]}, {"replacements": [{"block_id": [], "candidate_id": "c"}]},
                    {"replacements": [{"block_id": "wrong", "candidate_id": "c"}]}):
            with self.subTest(bad=bad):
                calls = []
                before = deepcopy(self.payload)
                def choose(context):
                    calls.append(context)
                    return bad
                with self.assertRaisesRegex(ReplanError, "完整的替换结果"):
                    self.run_plan(choose=choose)
                self.assertEqual(len(calls), 3)
                self.assertEqual(self.payload, before)

    def test_empty_and_truncated_output_retry(self):
        calls = []
        def choose(context):
            calls.append(context)
            if len(calls) == 1:
                return parse_selection(None)
            if len(calls) == 2:
                return parse_selection('{"replacements":', "length")
            return self.choose(context)
        self.assertEqual(self.run_plan(choose=choose)["revision"], 1)
        self.assertEqual(len(calls), 3)

    def test_selection_parser(self):
        self.assertEqual(parse_selection('```json\n{"replacements": []}\n```'), {"replacements": []})
        for content in (" ", "invalid", "[]", None):
            with self.subTest(content=content), self.assertRaises(ReplanError):
                parse_selection(content)

    def test_compact_context_retains_constraints_without_mutation(self):
        context = {"plan": deepcopy(self.original), "targets": [self.original["blocks"][0]],
                   "candidates": {"a": self.search["poi"]}, "instruction": "不去寺庙",
                   "profile": {"diet": "素食"}, "basic": {"total_budget": 1000}}
        context["plan"]["plans"] = [{"duplicate": "itinerary"}]
        before = deepcopy(context)
        compact = selection_context(context)
        self.assertNotIn("plans", compact["plan"])
        self.assertNotIn("legs", compact["plan"])
        self.assertEqual(compact["required_block_ids"], ["a"])
        for key in ("targets", "candidates", "profile", "basic", "instruction"):
            self.assertEqual(compact[key], context[key])
        self.assertEqual(context, before)

    def test_failure_or_success_never_mutates_input(self):
        before = deepcopy(self.payload)
        self.run_plan()
        self.assertEqual(before, self.payload)
        self.payload["locked_block_ids"] = ["b"]
        before = deepcopy(self.payload)
        with self.assertRaises(ReplanError):
            self.run_plan()
        self.assertEqual(before, self.payload)

    def test_preserves_other_style_and_unaffected_day(self):
        result = self.run_plan()["plan"]
        for bid in ("other", "d"):
            self.assertEqual(next(b for b in result["blocks"] if b["id"] == bid), next(b for b in self.original["blocks"] if b["id"] == bid))
        self.assertIn(self.original["legs"][0], result["legs"])

    def test_hotel_change_recalculates_next_day_departure(self):
        self.payload["target_block_ids"] = ["h"]
        result = self.run_plan()
        self.assertEqual(result["affected_days"], [1, 2])
        day_two = next(b for b in result["plan"]["blocks"] if b["id"] == "d")
        self.assertEqual(day_two["time"], "09:10-11:10")
        self.assertTrue(any(l["day"] == 2 and l["from"] == "h" for l in result["plan"]["legs"]))

    def test_missing_routes_preserves_original(self):
        with self.assertRaisesRegex(ReplanError, "交通路线"):
            self.run_plan(routes=lambda result, city: None)

    def test_no_candidates(self):
        self.search["poi"] = []
        with self.assertRaisesRegex(ReplanError, "替代项"):
            self.run_plan()

    def test_rejects_model_invented_candidate(self):
        with self.assertRaisesRegex(ReplanError, "搜索结果"):
            self.run_plan(choose=lambda _: {"replacements": [{"block_id": "a", "candidate_id": "invented"}]})

    def test_rejects_wrong_style_and_duplicate_ids(self):
        self.payload["target_block_ids"] = ["other"]
        with self.assertRaisesRegex(ReplanError, "当前方案"):
            self.run_plan()
        self.payload["target_block_ids"] = ["a"]
        self.original["blocks"].append(deepcopy(self.original["blocks"][0]))
        with self.assertRaisesRegex(ReplanError, "唯一"):
            self.run_plan()

    def test_stale_revision(self):
        self.payload["revision"] = 1
        with self.assertRaisesRegex(ReplanError, "版本"):
            self.run_plan()

    def test_budget_overflow(self):
        self.payload["basic"] = {"total_budget": 1}
        with self.assertRaisesRegex(ReplanError, "预算"):
            self.run_plan()

    def test_unknown_candidate_price_with_budget(self):
        self.payload["basic"] = {"total_budget": 1000}
        self.search["poi"][0].pop("price")
        with self.assertRaisesRegex(ReplanError, "缺少价格"):
            self.run_plan()

    def test_free_candidate_is_zero_not_unknown(self):
        self.assertEqual(replacement(self.original["blocks"][0], {"name": "免费公园", "price": 0})["price"], 0)
        self.assertEqual(replacement(self.original["blocks"][0], {"name": "免费公园", "free": True})["price"], 0)

    def test_new_place_does_not_inherit_old_coordinates_or_options(self):
        old = {**self.original["blocks"][0], "options": [{"name": "旧地点"}], "poi_id": "old"}
        new = replacement(old, {"name": "博物馆", "price": 10})
        for field in ("lng", "lat", "poi_id", "options"):
            self.assertNotIn(field, new)

    def test_opening_hours_conflict(self):
        self.search["poi"][0]["opening_hours"] = "15:00-16:00"
        with self.assertRaisesRegex(ReplanError, "营业时间"):
            self.run_plan()

    def test_retries_with_constraint_feedback(self):
        self.search["poi"][0]["opening_hours"] = "15:00-16:00"
        calls = []
        def choose(context):
            calls.append(context)
            return {"replacements": [{"block_id": "a", "candidate_id": "c0" if len(calls) == 1 else "c1"}]}
        result = self.run_plan(choose=choose)
        self.assertEqual(len(calls), 2)
        self.assertIn("营业时间", calls[1]["feedback"])
        self.assertEqual(result["plan"]["blocks"][0]["name"], "第二候选")

    def test_multiple_targets_keep_stable_ids(self):
        self.payload["target_block_ids"] = ["a", "b"]
        result = self.run_plan()
        self.assertEqual([b["id"] for b in result["plan"]["blocks"]], [b["id"] for b in self.original["blocks"]])
        self.assertEqual(result["plan"]["blocks"][1]["name"], "新餐厅")

    def test_unselected_previous_slot_keeps_original_time(self):
        self.payload["target_block_ids"] = ["b"]
        result = self.run_plan()
        self.assertEqual(result["plan"]["blocks"][0]["time"], "09:00-11:00")

    def test_event_on_other_date_is_excluded(self):
        self.assertEqual(catalog({"events": [{"title": "演唱会", "date": "2026-10-21"}]}, block("event", 1, "活动", "展览", "14:00-16:00")), [])

    def test_weather_is_not_replannable(self):
        self.original["blocks"][0]["type"] = "天气"
        with self.assertRaisesRegex(ReplanError, "类别"):
            self.run_plan()

    def test_rebuilt_nested_schedule_matches_blocks(self):
        result = self.run_plan()["plan"]
        nested = [item for p in result["plans"] for day in p["itinerary"] for item in day["schedule"]]
        self.assertEqual(nested, result["blocks"])

    def test_outbound_replacement_uses_arrival_station_and_delays_day(self):
        flight = block("flight", 1, "交通", "A1", "08:00-09:00")
        self.original["blocks"].insert(0, flight)
        self.payload["target_block_ids"] = ["flight"]
        self.search["flights"] = [
            {"airline": "A", "flight_no": "1", "dep_time": "2026-10-20 08:00", "arr_time": "2026-10-20 09:00", "arr_station": "杭州机场", "dep_station": "上海机场", "direction": "去", "price": 100},
            {"airline": "B", "flight_no": "2", "dep_time": "2026-10-20 11:00", "arr_time": "2026-10-20 12:00", "arr_station": "杭州机场", "dep_station": "上海机场", "direction": "去", "price": 100},
        ]
        result = self.run_plan()["plan"]
        self.assertEqual(result["blocks"][0]["name"], "B2")
        self.assertEqual(result["blocks"][0]["type"], "交通")
        self.assertEqual(result["blocks"][1]["time"], "14:10-16:10")
        self.assertTrue(any(l["from"] == "flight" for l in result["legs"]))

    def test_return_trip_needs_real_transfer_and_buffer(self):
        self.original["blocks"] = [b for b in self.original["blocks"] if b["id"] != "h"]
        self.original["blocks"].insert(2, block("flight", 1, "交通", "A1", "18:00-19:00"))
        self.payload["target_block_ids"] = ["flight"]
        self.search["flights"] = [
            {"airline": "A", "flight_no": "1", "dep_time": "2026-10-20 18:00", "arr_time": "2026-10-20 19:00", "arr_station": "上海机场", "dep_station": "杭州机场", "direction": "返", "price": 100},
            {"airline": "B", "flight_no": "2", "dep_time": "2026-10-20 14:00", "arr_time": "2026-10-20 15:00", "arr_station": "上海机场", "dep_station": "杭州机场", "direction": "返", "price": 100},
        ]
        with self.assertRaisesRegex(ReplanError, "固定出行时间"):
            self.run_plan()

    def test_airline_alias_and_evening_outbound_use_arrival_airport(self):
        flight = block("flight", 1, "交通", "厦航 MF3860（去程）", "20:25-21:50")
        self.original["blocks"].insert(0, flight)
        self.search["flights"] = [{"airline": "厦门航空", "flight_no": "MF3860",
            "dep_time": "2026-10-20 20:25", "arr_time": "2026-10-20 21:50",
            "dep_station": "长乐国际机场", "arr_station": "浦东国际机场", "direction": "去"}]
        # Route boundaries must use flight direction, not time of day.
        from replan import route_stops
        stops = route_stops(self.original["blocks"], "推荐方案", self.search)
        self.assertEqual(stops[0]["name"], "浦东国际机场")
        self.assertEqual(travel_metadata(flight, self.search)["direction"], "去")

    def test_legacy_station_note_works_without_flight_in_new_search(self):
        flight = block("flight", 1, "交通", "旧航班 MU1234 去程", "07:00-08:00")
        flight["note"] = "虹桥国际机场 → 萧山国际机场"
        self.original["blocks"].insert(0, flight)
        result = self.run_plan()["plan"]
        self.assertTrue(any(leg["from"] == "flight" for leg in result["legs"]))
        self.assertEqual(result["blocks"][0]["arr_station"], "萧山国际机场")

    def test_local_transfer_is_repaired_as_real_leg_not_airport(self):
        local = block("local", 1, "交通", "打车前往午餐店", "11:00-11:25")
        self.original["blocks"].insert(1, local)
        result = self.run_plan()["plan"]
        by_id = {b["id"]: b for b in result["blocks"]}
        self.assertEqual(by_id["local"]["time"], "11:00-12:10")
        self.assertEqual(by_id["local"]["name"], "博物馆 → 午餐店")
        self.assertEqual(by_id["b"]["time"], "12:10-13:10")
        self.assertFalse(any(l["from"] == "local" or l["to"] == "local" for l in result["legs"]))

    def test_missing_airport_reports_specific_block(self):
        self.original["blocks"].insert(0, block("flight", 1, "交通", "MU1234", "07:00-08:00"))
        with self.assertRaisesRegex(ReplanError, "MU1234.*机场或车站"):
            self.run_plan()

    def test_same_flight_on_different_dates_and_ambiguous_airports(self):
        flight = block("flight", 1, "交通", "厦航MF3860", "08:00-09:00")
        row = {"airline": "厦门航空", "flight_no": "MF3860", "dep_time": "2026-10-20 08:00",
               "arr_time": "2026-10-20 09:00", "dep_station": "长乐国际机场", "arr_station": "浦东国际机场"}
        search = {"flights": {"outbound": [row, {**row, "dep_time": "2026-10-21 08:00"}], "inbound": None}}
        self.assertEqual(match_travel(flight, search)["dep_time"], "2026-10-20 08:00")
        search["flights"]["outbound"].append({**row, "arr_station": "虹桥国际机场"})
        self.assertEqual(match_travel(flight, search), {})

    def test_transport_number_does_not_match_longer_number(self):
        flight = block("flight", 1, "交通", "MU12345", "08:00-09:00")
        self.assertEqual(match_travel(flight, {"flights": [{"flight_no": "MU1234"}]}), {})

    def test_blockify_preserves_transport_metadata(self):
        import ast
        path = Path(__file__).resolve().parents[1] / "plan.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "blockify"]
        namespace = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        item = {"type": "交通", "name": "MF3860", "time": "08:00-09:00", "dep_station": "长乐国际机场",
                "arr_station": "浦东国际机场", "flight_no": "MF3860", "direction": "去"}
        result = namespace["blockify"]({"plans": [{"style": "推荐", "itinerary": [{"day": 1, "schedule": [item]}]}]})
        for field in ("dep_station", "arr_station", "flight_no", "direction"):
            self.assertEqual(result[0][field], item[field])

    def test_continuous_hotel_stays_are_replaced_together(self):
        self.original["blocks"].insert(4, block("h2", 2, "酒店", "酒店甲", "18:00-18:30"))
        self.payload["target_block_ids"] = ["h"]
        result = self.run_plan()["plan"]
        self.assertEqual([b["name"] for b in result["blocks"] if b["type"] == "酒店"], ["酒店乙", "酒店乙"])

    def test_late_day_overflow_fails(self):
        self.original["blocks"][0]["time"] = "22:00-23:00"
        with self.assertRaisesRegex(ReplanError, "23:30"):
            self.run_plan()

    def test_no_stay_label_does_not_require_geocoding(self):
        self.original["blocks"][2]["name"] = "当晚返程，无住宿"
        result = self.run_plan()
        self.assertEqual(result["plan"]["blocks"][2], self.original["blocks"][2])


    def test_replacement_clears_stale_price_basis_and_counts_travelers(self):
        self.original["blocks"][0].update(unit_price=999, price=999, price_basis="group", price_known=True, price_source="manual")
        self.payload["basic"] = {"travelers": "2人", "total_budget": 1000}
        result = self.run_plan()["plan"]
        target = next(b for b in result["blocks"] if b["id"] == "a")
        self.assertEqual(target["unit_price"], 30)
        self.assertEqual(target["price"], 60)
        self.assertEqual(target["price_basis"], "per_person")
        self.assertEqual(target["price_source"], "search")
        self.assertTrue(target["price_known"])
        self.assertEqual(result["cost_by_style"]["推荐方案"], 120)

    def test_hotel_quote_is_group_cost_even_with_multiple_travelers(self):
        self.payload["target_block_ids"] = ["h"]
        self.payload["basic"] = {"travelers": "3人"}
        target = next(b for b in self.run_plan()["plan"]["blocks"] if b["id"] == "h")
        self.assertEqual(target["price"], 50)
        self.assertEqual(target["unit_price"], 50)
        self.assertEqual(target["price_basis"], "group")

    def test_known_zero_placeholder_does_not_hide_unknown_price(self):
        self.original["blocks"][1].update(price=0, unit_price=None, price_known=False, price_source="unknown")
        result = self.run_plan()["plan"]
        self.assertEqual(result["budget_by_style"]["推荐方案"], "unknown")
        self.assertEqual(result["unpriced_items"]["推荐方案"], ["午餐店"])
        self.assertIn("b", result["unknown_price_block_ids"])

    def test_unpriced_other_style_does_not_change_current_budget_status(self):
        self.original["blocks"][-1].update(price=0, price_known=False)
        result = self.run_plan()["plan"]
        self.assertEqual(result["budget_status"], "ok")
        self.assertEqual(result["budget_by_style"]["其他方案"], "unknown")
        self.assertEqual(result["blocks"][-1], self.original["blocks"][-1])

    def test_activity_window_rejects_replacement_before_fixed_departure(self):
        self.original["blocks"][0]["activity_window"] = {"start_min": 9 * 60, "end_min": 10 * 60}
        before = deepcopy(self.payload)
        with self.assertRaisesRegex(ReplanError, "可活动时间"):
            self.run_plan()
        self.assertEqual(self.payload, before)

    def test_meal_refresh_is_limited_to_affected_style_and_days(self):
        calls = []
        result = replan_plan(self.payload, self.search, self.choose, self.routes,
                             lambda plan: calls.append(deepcopy(plan["blocks"])))
        self.assertEqual(len(calls), 1)
        self.assertEqual({b["day"] for b in calls[0]}, {1})
        self.assertEqual({b["plan_style"] for b in calls[0]}, {"推荐方案"})
        self.assertEqual(result["revision"], 1)


if __name__ == "__main__":
    unittest.main()
