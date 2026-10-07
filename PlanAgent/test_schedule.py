"""离线调度回归：真实班次和每天可用时间必须一致。"""

import copy
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan


def train(number, direction, departure, arrival):
    return {
        "train_no": number, "transport": "高铁", "direction": direction,
        "dep_time": departure, "arr_time": arrival,
        "dep_station": "上海虹桥" if direction == "去" else "杭州东",
        "arr_station": "杭州东" if direction == "去" else "上海虹桥",
        "price": 73, "url": f"https://example.test/train/{number}",
    }


def flight(number, direction, departure, arrival):
    return {
        "flight_no": number, "airline": "测试航空", "direction": direction,
        "dep_time": departure, "arr_time": arrival,
        "dep_station": "上海机场" if direction == "去" else "杭州机场",
        "arr_station": "杭州机场" if direction == "去" else "上海机场",
        "price": 600, "url": f"https://example.test/flight/{number}",
    }


def selected(source, kind):
    return {
        **source, "source": copy.deepcopy(source), "kind": kind,
        "dep_dt": source["dep_time"].replace(" ", "T"),
        "arr_dt": source["arr_time"].replace(" ", "T"),
    }


def trip_search():
    return {
        "origin": "上海", "destination": "杭州",
        "start_date": "2026-10-16", "end_date": "2026-10-17",
        "trains": [
            train("G219", "去", "2026-10-16 07:04:00", "2026-10-16 07:49:00"),
            train("D3131", "去", "2026-10-16 07:41:00", "2026-10-16 08:37:00"),
            train("G1350", "回", "2026-10-17 22:02:00", "2026-10-17 23:01:00"),
        ],
        "flights": [
            flight("MU9001", "去", "2026-10-16 23:30:00", "2026-10-17 00:40:00"),
            flight("MU9002", "回", "2026-10-17 06:20:00", "2026-10-17 07:30:00"),
        ],
        "poi": [
            {"name": "西湖风景名胜区", "duration": "4小时", "longitude": 120.145,
             "latitude": 30.251, "url": "https://example.test/poi/west-lake"},
            {"name": "浙江省博物馆", "duration": "2小时", "longitude": 120.147,
             "latitude": 30.259, "url": "https://example.test/poi/museum"},
            {"name": "西湖天地", "duration": "1小时", "longitude": 120.159,
             "latitude": 30.243, "url": "https://example.test/poi/shopping"},
        ],
    }


def interval(item):
    times = re.findall(r"(\d{1,2}):(\d{2})", str(item.get("time") or ""))
    if len(times) < 2:
        return None
    return tuple(int(hour) * 60 + int(minute) for hour, minute in times[:2])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        # 如果调度逻辑意外调用模型、地图或子进程，测试立即失败。
        for target in ("plan.OpenAI", "plan.geocode", "plan.subprocess.run"):
            mocked = patch(target, side_effect=AssertionError("调度测试禁止网络和子进程"))
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_short_verified_train_beats_overnight_flight_and_early_return(self):
        search = trip_search()
        transport = plan._select_transport(search)
        self.assertEqual(transport["outbound"]["kind"], "train")
        self.assertEqual(transport["outbound"]["train_no"], "G219")
        self.assertEqual(transport["outbound"]["dep_dt"], "2026-10-16T07:04:00")
        self.assertEqual(transport["outbound"]["arr_dt"], "2026-10-16T07:49:00")
        self.assertEqual(transport["inbound"]["train_no"], "G1350")
        self.assertEqual(transport["inbound"]["dep_dt"], "2026-10-17T22:02:00")
        for field, value in search["trains"][0].items():
            self.assertEqual(transport["outbound"][field], value)

    def test_invalid_or_wrong_date_records_cannot_be_selected(self):
        search = trip_search()
        search["flights"] = []
        search["trains"] = [
            train("缺到达时间", "去", "2026-10-16 07:00:00", ""),
            train("日期错误", "去", "2026-10-15 07:00:00", "2026-10-15 08:00:00"),
            train("倒流", "回", "2026-10-17 18:00:00", "2026-10-17 17:00:00"),
        ]
        result = plan._select_transport(search)
        self.assertFalse(result.get("outbound"))
        self.assertFalse(result.get("inbound"))

    def test_activity_window_reserves_transfer_time_for_actual_train(self):
        transport = plan._select_transport(trip_search())
        self.assertEqual(plan._activity_window("2026-10-16", transport), (8 * 60 + 49, 20 * 60))
        self.assertEqual(plan._activity_window("2026-10-17", transport), (8 * 60 + 30, 20 * 60))
        self.assertEqual(plan._activity_window("2026-10-16", {}), (8 * 60 + 30, 20 * 60))

    def test_flight_window_uses_two_hour_buffer_and_cross_day_arrival(self):
        same_day = {
            "outbound": selected(flight("去程", "去", "2026-10-16 08:00:00", "2026-10-16 10:00:00"), "flight"),
            "inbound": selected(flight("回程", "回", "2026-10-16 18:00:00", "2026-10-16 19:00:00"), "flight"),
        }
        self.assertEqual(plan._activity_window("2026-10-16", same_day), (12 * 60, 16 * 60))
        overnight = {"outbound": selected(trip_search()["flights"][0], "flight")}
        start, end = plan._activity_window("2026-10-16", overnight)
        self.assertGreaterEqual(start, end, "到达在次日，首日不能安排游览")
        self.assertEqual(plan._activity_window("2026-10-17", overnight), (8 * 60 + 30, 20 * 60))

    def test_unused_late_flight_does_not_make_selected_early_train_late(self):
        search = trip_search()
        self.assertFalse(plan._first_day_arrives_late(search))
        self.assertTrue(plan._first_day_arrives_late({
            "outbound": selected(search["flights"][0], "flight"),
            "inbound": None,
        }))

    def test_enforcement_uses_assignment_day_date_and_verified_train(self):
        search = trip_search()
        assignment = {"day": 1, "names": ["西湖风景名胜区"]}
        day_plan = {"day": 99, "date": "2026-10-17", "schedule": [
            {"type": "交通", "name": "模型编造早班飞机", "time": "05:00-06:00"},
            {"type": "景点", "name": "西湖风景名胜区", "time": "06:00-10:00"},
        ]}
        result = plan._enforce_day_schedule(day_plan, assignment, "2026-10-16", plan._select_transport(search), search)
        self.assertEqual(result["day"], 1)
        self.assertEqual(result["date"], "2026-10-16")
        transports = [item for item in result["schedule"] if item.get("type") == "交通"]
        self.assertTrue(any("G219" in item["name"] and interval(item) == (7 * 60 + 4, 7 * 60 + 49) for item in transports))
        self.assertFalse(any("编造" in item["name"] for item in result["schedule"]))
        spots = [item for item in result["schedule"] if item.get("type") == "景点"]
        self.assertEqual([item["name"] for item in spots], ["西湖风景名胜区"])
        self.assertGreaterEqual(interval(spots[0])[0], 8 * 60 + 49)
        self.assertLessEqual(interval(spots[0])[1], 20 * 60)

    def test_early_return_does_not_leave_attractions_after_departure(self):
        search = trip_search()
        early = train("早回", "回", "2026-10-17 06:00:00", "2026-10-17 07:00:00")
        transport = {"inbound": selected(early, "train")}
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "浙江省博物馆", "time": "09:00-11:00"}]},
            {"day": 2, "names": ["浙江省博物馆"]},
            "2026-10-17", transport, search,
        )
        self.assertEqual([item for item in result["schedule"] if item.get("type") == "景点"], [])
        transports = [item for item in result["schedule"] if item.get("type") == "交通"]
        self.assertTrue(any("早回" in item["name"] and interval(item) == (6 * 60, 7 * 60) for item in transports))

    def test_overlapping_spots_are_rescheduled_without_overlap(self):
        search = trip_search()
        result = plan._enforce_day_schedule(
            {"schedule": [
                {"type": "景点", "name": "西湖天地", "time": "09:00-14:00"},
                {"type": "景点", "name": "浙江省博物馆", "time": "10:00-12:00"},
            ]},
            {"day": 2, "names": ["西湖天地", "浙江省博物馆"]},
            "2026-10-17", plan._select_transport(search), search,
        )
        spots = [item for item in result["schedule"] if item.get("type") == "景点"]
        self.assertIn("浙江省博物馆", [item["name"] for item in spots])
        timed = sorted(interval(item) for item in spots)
        for start, end in timed:
            self.assertGreaterEqual(start, 8 * 60 + 30)
            self.assertGreater(end, start)
            self.assertLessEqual(end, 20 * 60)
        for before, after in zip(timed, timed[1:]):
            self.assertLessEqual(before[1], after[0])

    def test_missing_transport_does_not_preserve_model_invented_departure(self):
        result = plan._enforce_day_schedule(
            {"schedule": [
                {"type": "交通", "name": "高铁G9999", "time": "07:00-08:00"},
                {"type": "景点", "name": "浙江省博物馆", "time": "10:00-12:00"},
            ]},
            {"day": 1, "names": ["浙江省博物馆"]},
            "2026-10-16", {}, {"poi": trip_search()["poi"]},
        )
        self.assertFalse(any(item.get("type") == "交通" and interval(item) for item in result["schedule"]))
        self.assertTrue(any(item["name"] == "浙江省博物馆" for item in result["schedule"]))

    def assert_meal_slot(self, result, kind):
        slot = plan._slot_time(result["schedule"], kind, result["activity_window"])
        self.assertTrue(slot, f"{kind}应有可用空档")
        meal = interval({"time": slot})
        self.assertIsNotNone(meal)
        self.assertGreaterEqual(meal[1] - meal[0], 60)
        window = (11 * 60, 14 * 60) if kind == "午餐" else (17 * 60, 20 * 60)
        self.assertGreaterEqual(meal[0], max(window[0], result["activity_window"]["start_min"]))
        self.assertLessEqual(meal[1], min(window[1], result["activity_window"]["end_min"]))
        for item in result["schedule"]:
            occupied = interval(item)
            if occupied:
                self.assertTrue(meal[1] <= occupied[0] or meal[0] >= occupied[1],
                                f"{kind}{slot}不能与{item['name']} {item['time']}重叠")
        return meal

    def test_full_day_attraction_keeps_an_hour_for_lunch(self):
        search = trip_search()
        search["poi"][0]["duration"] = "全天"
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "西湖风景名胜区", "time": "09:00-15:00"}]},
            {"day": 1, "names": ["西湖风景名胜区"]},
            "2026-10-16", {}, search,
        )
        self.assertTrue(any(item["name"] == "西湖风景名胜区" for item in result["schedule"]))
        self.assert_meal_slot(result, "午餐")

    def test_adjacent_attractions_keep_lunch_and_transfer_time(self):
        search = trip_search()
        search["poi"][0]["duration"] = "3小时"
        result = plan._enforce_day_schedule(
            {"schedule": [
                {"type": "景点", "name": "西湖风景名胜区", "time": "09:00-12:00"},
                {"type": "景点", "name": "浙江省博物馆", "time": "12:30-14:30"},
            ]},
            {"day": 1, "names": ["西湖风景名胜区", "浙江省博物馆"]},
            "2026-10-16", {}, search,
        )
        spots = [item for item in result["schedule"] if item.get("type") == "景点"]
        self.assertCountEqual([item["name"] for item in spots], ["西湖风景名胜区", "浙江省博物馆"])
        first, second = sorted(interval(item) for item in spots)
        self.assertGreaterEqual(second[0] - first[1], 90, "午餐60分钟之外还要预留接驳")
        self.assert_meal_slot(result, "午餐")

    def test_late_afternoon_attraction_finishes_before_dinner(self):
        search = trip_search()
        search["poi"][1]["duration"] = "3小时"
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "浙江省博物馆", "time": "17:00-20:00"}]},
            {"day": 1, "names": ["浙江省博物馆"]},
            "2026-10-16", {}, search,
        )
        spots = [item for item in result["schedule"] if item.get("type") == "景点"]
        self.assertEqual(len(spots), 1)
        self.assertLessEqual(interval(spots[0])[1], 19 * 60)
        self.assert_meal_slot(result, "晚餐")

    def test_arrival_during_lunch_reserves_meal_before_first_attraction(self):
        search = trip_search()
        transport = {"outbound": selected(train(
            "午间到达", "去", "2026-10-16 10:30:00", "2026-10-16 11:30:00",
        ), "train"), "inbound": None}
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "浙江省博物馆", "time": "12:30-14:30"}]},
            {"day": 1, "names": ["浙江省博物馆"]},
            "2026-10-16", transport, search,
        )
        self.assertEqual(result["activity_window"]["start_min"], 12 * 60 + 30)
        meal = self.assert_meal_slot(result, "午餐")
        self.assertEqual(meal, (12 * 60 + 30, 13 * 60 + 30))
        spots = [item for item in result["schedule"] if item.get("type") == "景点"]
        self.assertEqual(len(spots), 1)
        self.assertGreaterEqual(interval(spots[0])[0], meal[1])

    def test_proximity_ordering_keeps_each_attraction_on_its_assigned_day(self):
        itinerary = [{"day": 1, "date": "2026-10-16", "schedule": [
            {"type": "景点", "name": "西湖风景名胜区", "time": "09:00-13:00", "lng": 120.145, "lat": 30.251},
        ]}, {"day": 2, "date": "2026-10-17", "schedule": [
            {"type": "景点", "name": "浙江省博物馆", "time": "09:00-11:00", "lng": 120.147, "lat": 30.259},
        ]}]
        original = copy.deepcopy(itinerary)
        with patch.dict(os.environ, {"AMAP_KEY": "test-only"}):
            result = plan._reorder_by_proximity({"plans": [{"style": "推荐方案", "itinerary": itinerary}]}, "杭州")
        self.assertEqual(result["plans"][0]["itinerary"], original)

    def test_model_price_cannot_turn_unpriced_attraction_into_known_cost(self):
        search = trip_search()
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "西湖风景名胜区", "time": "09:00-11:00",
                           "price": 55, "unit_price": 55, "price_known": True,
                           "price_basis": "per_person", "price_source": "model"}]},
            {"day": 1, "names": ["西湖风景名胜区"]}, "2026-10-16", {}, search,
        )
        scene = result["schedule"][0]
        for field in ("price", "unit_price", "price_known", "price_basis", "price_source"):
            self.assertNotIn(field, scene)

    def test_only_source_price_or_explicit_free_flag_is_used(self):
        for source, expected in (({"price": "¥30.00"}, 30), ({"free": True}, 0),
                                 ({"price": "8xx", "free": False}, None)):
            with self.subTest(source=source):
                search = trip_search()
                search["poi"][0].update(source)
                result = plan._enforce_day_schedule(
                    {"schedule": [{"type": "景点", "name": "西湖风景名胜区", "time": "09:00-11:00", "price": 55}]},
                    {"day": 1, "names": ["西湖风景名胜区"]}, "2026-10-16", {}, search,
                )
                scene = result["schedule"][0]
                self.assertEqual(scene.get("unit_price"), expected)
                self.assertEqual(scene.get("price_known"), True if expected is not None else None)

    def test_optional_night_market_is_after_five_and_keeps_dinner(self):
        search = trip_search()
        search["poi"].append({"name": "吴山夜市", "duration": "2小时", "longitude": 120.148, "latitude": 30.252})
        result = plan._enforce_day_schedule(
            {"schedule": [
                {"type": "景点", "name": "浙江省博物馆", "time": "09:00-11:00"},
                {"type": "景点", "name": "吴山夜市", "time": "15:00-17:00"},
            ]},
            {"day": 1, "names": ["浙江省博物馆", "吴山夜市"]},
            "2026-10-16", {}, search,
        )
        market = next(item for item in result["schedule"] if item["name"] == "吴山夜市")
        self.assertGreaterEqual(interval(market)[0], 17 * 60)
        self.assertIn("核实", market["note"])
        self.assert_meal_slot(result, "晚餐")

    def test_night_market_respects_verified_hours_and_activity_window(self):
        search = trip_search()
        search["poi"].append({"name": "吴山夜市", "duration": "2小时", "opening_hours": "18:00-19:00"})
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "吴山夜市", "time": "15:00-17:00"}]},
            {"day": 1, "names": ["吴山夜市"]}, "2026-10-16", {}, search,
        )
        self.assertEqual(interval(result["schedule"][0]), (18 * 60, 19 * 60))
        self.assert_meal_slot(result, "晚餐")

    def test_night_market_cannot_be_moved_to_afternoon_to_fit_early_return(self):
        search = trip_search()
        search["poi"].append({"name": "吴山夜市", "duration": "2小时"})
        transport = {"outbound": None, "inbound": selected(train("早返程", "回", "2026-10-17 17:00:00", "2026-10-17 18:00:00"), "train")}
        result = plan._enforce_day_schedule(
            {"schedule": [{"type": "景点", "name": "吴山夜市", "time": "14:00-16:00"}]},
            {"day": 2, "names": ["吴山夜市"]}, "2026-10-17", transport, search,
        )
        self.assertFalse(any(item["name"] == "吴山夜市" for item in result["schedule"]))

    def test_return_without_accommodation_is_not_a_hotel_block(self):
        result = plan.blockify({"plans": [{"style": "推荐方案", "itinerary": [{
            "day": 2, "date": "2026-10-17", "hotel": "当晚返程，无住宿",
            "schedule": [
                {"type": "酒店", "name": "当晚返程，无住宿", "time": "住宿"},
                {"type": "交通", "name": "高铁G1350", "time": "22:02-23:01"},
            ],
        }]}]})
        self.assertFalse(any(item["type"] == "酒店" for item in result))
        self.assertEqual([item["name"] for item in result], ["高铁G1350"])

    def test_pace_maps_to_daily_spot_range(self):
        self.assertEqual(plan._pace_spots("慢", []), (2, 3))
        self.assertEqual(plan._pace_spots("轻松", []), (3, 4))
        self.assertEqual(plan._pace_spots("休闲", []), (3, 4))
        self.assertEqual(plan._pace_spots("适中", []), (3, 4))
        self.assertEqual(plan._pace_spots("", []), (3, 4))
        self.assertEqual(plan._pace_spots("快", []), (4, 6))
        self.assertEqual(plan._pace_spots("紧凑", []), (4, 6))
        self.assertEqual(plan._pace_spots("", ["休闲度假"]), (3, 4))

    def test_first_day_checks_into_hotel_before_sightseeing(self):
        day = {
            "day": 1,
            "hotel": "测试酒店",
            "activity_window": {"start_min": 8 * 60 + 30},
            "schedule": [
                {"type": "交通", "name": "去程：高铁G219", "time": "07:04-07:49"},
                {"type": "景点", "name": "西湖", "time": "09:00-11:00"},
            ],
        }
        plan._add_first_day_checkin(day)
        self.assertEqual([s["type"] for s in day["schedule"]], ["交通", "酒店", "景点"])
        self.assertEqual(day["schedule"][1]["time"], "07:49-08:19")
        self.assertEqual(day["schedule"][1]["name"], "测试酒店")
        plan._add_first_day_checkin(day)
        self.assertEqual([s["type"] for s in day["schedule"]], ["交通", "酒店", "景点"])


class EveningActivityTests(unittest.TestCase):
    def _day(self):
        return {
            "day": 1,
            "hotel": "测试酒店",
            "activity_window": {"end_min": 22 * 60},
            "schedule": [
                {"type": "美食", "meal": "晚餐", "time": "18:00-19:00", "name": "某餐厅"},
            ],
        }

    def test_evening_activity_avoids_already_used_poi(self):
        search = {
            "destination": "武汉",
            "poi": [
                {"name": "洪山广场", "longitude": 120.00, "latitude": 30.00},
                {"name": "光谷步行街", "longitude": 120.01, "latitude": 30.01},
            ],
        }
        hotel_map = {"测试酒店": {"longitude": 120.005, "latitude": 30.005}}
        day = self._day()
        with patch("plan._hotel_coord", return_value=[120.005, 30.005]):
            plan._add_evening_activity(day, search, hotel_map, {"age_group": "18-25"},
                                       global_used_names={"洪山广场"})
        activity_names = [s["name"] for s in day["schedule"] if s.get("type") == "活动"]
        self.assertEqual(activity_names, ["光谷步行街"])
        self.assertNotIn("洪山广场", activity_names)


if __name__ == "__main__":
    unittest.main()
