"""A slower automatic mode cannot hide a verified feasible local transfer."""
import copy
import unittest
from unittest.mock import patch

import plan
import route_map
from ValidateAgent.rules import validate_rules


class LocalRouteModeTests(unittest.TestCase):
    def sample(self, start="13:40-14:10"):
        return {"destination":"杭州", "start_date":"2026-10-16", "end_date":"2026-10-16", "blocks":[
            {"id":"meal", "name":"翠薇面斋", "type":"美食", "plan_style":"经典", "day":1,
             "date":"2026-10-16", "time":"12:00-13:00", "price":0, "price_known":True,
             "_geo":{"location":"120.104366,30.240891", "citycode":"0571"}},
            {"id":"park", "name":"太子湾公园", "type":"景点", "plan_style":"经典", "day":1,
             "date":"2026-10-16", "time":start, "price":0, "price_known":True,
             "_geo":{"location":"120.142177,30.225470", "citycode":"0571"}}]}

    def route(self, sample, primary, driving):
        with patch.object(route_map, "route_leg", return_value=primary), \
             patch.object(route_map, "driving_travel", return_value=driving) as query:
            result = route_map._route_for_schedule(*sample["blocks"])
        return result, query

    def test_screenshot_transfer_uses_actual_driving_route_and_keeps_default_cache(self):
        sample = self.sample()
        transit = {"mode":"transit", "duration_s":2708, "distance_m":7348, "polyline":[[1,2],[3,4]]}
        driving = {"mode":"drive", "duration_s":848, "distance_m":6828, "polyline":[[5,6],[7,8]]}
        original = copy.deepcopy(transit)
        result, query = self.route(sample, transit, driving)
        self.assertEqual(result["mode"], "drive")
        self.assertEqual(result["duration_s"], 848)
        self.assertEqual(result["polyline"], driving["polyline"])
        self.assertEqual(result["alternatives"][0]["duration_s"], 2708)
        self.assertEqual(transit, original)
        query.assert_called_once_with("120.104366,30.240891", "120.142177,30.225470")

    def test_attached_map_and_reviewer_share_feasible_mode_without_changing_times(self):
        sample = self.sample()
        original_times = [b["time"] for b in sample["blocks"]]
        with patch.dict("os.environ", {"AMAP_KEY":"test"}), patch.object(route_map, "geocode_blocks"), \
             patch.object(route_map, "_save_cache"), \
             patch.object(route_map, "route_leg", return_value={"mode":"transit", "duration_s":2708}), \
             patch.object(route_map, "driving_travel", return_value={"mode":"drive", "duration_s":848, "polyline":[[1,2],[3,4]]}):
            route_map.attach_routes(sample, "杭州")
        plan._align_route_times(sample, adjust=True)
        review = validate_rules(sample, search={"food":[{"name":"翠薇面斋"}], "poi":[{"name":"太子湾公园"}]})
        self.assertEqual(sample["legs"][0]["mode"], "drive")
        self.assertEqual([b["time"] for b in sample["blocks"]], original_times)
        self.assertIn("需驾车/打车", sample["blocks"][1]["note"])
        self.assertEqual(sample["travel_time_warnings"], [])
        self.assertTrue(review["passed"])
        self.assertFalse(any(i["type"] == "交通" for i in review["issues"]))

    def test_already_sufficient_route_does_not_request_another_mode(self):
        route = {"mode":"transit", "duration_s":1200}
        result, query = self.route(self.sample(), route, {"mode":"drive", "duration_s":848})
        self.assertEqual(result, route)
        query.assert_not_called()

    def test_short_walk_can_switch_to_verified_driving(self):
        result, _ = self.route(self.sample("13:15-14:00"), {"mode":"walk", "duration_s":1200},
                               {"mode":"drive", "duration_s":360})
        self.assertEqual(result["mode"], "drive")

    def test_failed_alternative_lookup_does_not_invent_a_faster_route(self):
        route = {"mode":"transit", "duration_s":2708}
        result, _ = self.route(self.sample(), route, None)
        self.assertEqual(result, route)

    def test_faster_driving_that_still_does_not_fit_remains_a_real_conflict(self):
        sample = self.sample("13:10-14:00")
        result, _ = self.route(sample, {"mode":"transit", "duration_s":2708}, {"mode":"drive", "duration_s":848})
        sample["legs"] = [{"from":"meal", "to":"park", **result}]
        review = validate_rules(sample, search={"food":[{"name":"翠薇面斋"}], "poi":[{"name":"太子湾公园"}]})
        self.assertEqual(result["mode"], "drive")
        self.assertTrue(any(i["type"] == "交通" and i["severity"] == "high" for i in review["issues"]))

    def test_fixed_mode_and_cross_day_or_unscheduled_stops_do_not_switch(self):
        route = {"mode":"transit", "duration_s":2708, "mode_locked":True}
        _, query = self.route(self.sample(), route, {"mode":"drive", "duration_s":848})
        query.assert_not_called()
        for update in ({"day":2}, {"date":"2026-10-17"}, {"time":"下午"}, {"type":"交通"}, {"type":"酒店"}):
            with self.subTest(update=update):
                sample = self.sample()
                sample["blocks"][1].update(update)
                _, query = self.route(sample, {"mode":"transit", "duration_s":2708}, {"mode":"drive", "duration_s":848})
                query.assert_not_called()
