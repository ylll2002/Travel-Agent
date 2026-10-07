"""餐饮搜索智能体测试。

验证：
1. 高德地图 POI 搜索能返回结构化餐厅数据
2. 餐厅数据包含规划智能体所需的全部字段（含地图链接和 POI 详情链接）
3. 地图链接和 POI 详情链接格式正确
4. tools._fetch_food 能正确调用高德并在失败时降级到 Tavily
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

# 确保能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from amap_service import (  # noqa: E402
    RestaurantInfo,
    _build_map_url,
    _build_poi_detail_url,
    _parse_cuisine,
    search_restaurants,
)


def _has_amap_key() -> bool:
    return bool(os.getenv("AMAP_KEY")) and os.getenv("RUN_AMAP_INTEGRATION_TESTS") == "1"


@unittest.skipUnless(_has_amap_key(), "设置 RUN_AMAP_INTEGRATION_TESTS=1 才运行真实高德测试")
class TestAmapRestaurantSearch(unittest.TestCase):
    """高德地图餐厅搜索集成测试。"""

    def test_search_returns_structured_data(self):
        restaurants = search_restaurants("杭州", limit=10)
        self.assertGreater(len(restaurants), 0, "杭州应该能搜到餐厅")

        for r in restaurants:
            self.assertTrue(r.name, f"餐厅名不能为空: {r}")
            self.assertIsInstance(r.longitude, float)
            self.assertIsInstance(r.latitude, float)
            self.assertTrue(r.address, f"地址不能为空: {r.name}")
            # 经纬度合理范围（中国境内）
            self.assertTrue(70 < r.longitude < 140, f"经度异常: {r.longitude}")
            self.assertTrue(15 < r.latitude < 55, f"纬度异常: {r.latitude}")

    def test_restaurant_has_required_fields(self):
        restaurants = search_restaurants("杭州", limit=5)
        r = restaurants[0]
        d = r.to_dict()
        required = {
            "name", "address", "longitude", "latitude", "cuisine",
            "rating", "price_per_person", "business_area",
            "poi_id", "map_url", "poi_detail_url",
        }
        self.assertEqual(set(d.keys()), required)

    def test_map_url_format(self):
        url = _build_map_url(120.1551, 30.2741, "楼外楼")
        self.assertIn("uri.amap.com/marker", url)
        self.assertIn("position=120.1551,30.2741", url)
        self.assertIn("name=", url)

    def test_poi_detail_url_format(self):
        self.assertEqual(
            _build_poi_detail_url("B0FFFABCD"),
            "https://www.amap.com/place/B0FFFABCD",
        )
        self.assertEqual(_build_poi_detail_url(""), "")

    def test_restaurant_has_links(self):
        """每家餐厅都必须有地图链接和 POI 详情链接。"""
        restaurants = search_restaurants("杭州", limit=5)
        for r in restaurants:
            self.assertTrue(r.map_url, f"{r.name} 缺少地图链接")
            self.assertTrue(r.poi_detail_url, f"{r.name} 缺少 POI 详情链接")
            self.assertTrue(r.map_url.startswith("https://uri.amap.com/marker"))
            self.assertTrue(r.poi_detail_url.startswith("https://www.amap.com/place/"))

    def test_cuisine_parsing(self):
        self.assertEqual(_parse_cuisine("餐饮服务;中餐厅;川菜"), "川菜")
        self.assertEqual(_parse_cuisine("餐饮服务;外国餐厅;日本料理"), "日本料理")
        self.assertEqual(_parse_cuisine("餐饮服务;快餐厅"), "快餐厅")
        self.assertEqual(_parse_cuisine(""), "")

    def test_keyword_search(self):
        restaurants = search_restaurants("杭州", keyword="火锅", limit=5)
        self.assertGreater(len(restaurants), 0)
        cuisines = " ".join(r.cuisine for r in restaurants)
        names = " ".join(r.name for r in restaurants)
        self.assertTrue(
            "火锅" in cuisines or "火锅" in names,
            f"关键词搜索结果应含火锅: {cuisines} / {names}",
        )

    def test_to_dict_serializable(self):
        restaurants = search_restaurants("杭州", limit=3)
        for r in restaurants:
            d = r.to_dict()
            json.dumps(d, ensure_ascii=False)


class TestRestaurantInfoLocal(unittest.TestCase):
    """本地单元测试，不依赖网络。"""

    def test_default_values(self):
        r = RestaurantInfo(name="测试", address="测试地址", longitude=120.0, latitude=30.0)
        self.assertEqual(r.cuisine, "")
        self.assertIsNone(r.rating)
        self.assertIsNone(r.price_per_person)
        self.assertEqual(r.poi_id, "")
        self.assertEqual(r.map_url, "")
        self.assertEqual(r.poi_detail_url, "")

    def test_to_dict_contains_all_fields(self):
        r = RestaurantInfo(
            name="楼外楼",
            address="杭州市西湖区孤山路30号",
            longitude=120.1551,
            latitude=30.2741,
            cuisine="江浙菜",
            rating=4.5,
            price_per_person=188.0,
            business_area="西湖",
            poi_id="B0FFFABCD",
            map_url="https://uri.amap.com/marker?position=120.1551,30.2741&name=楼外楼",
            poi_detail_url="https://www.amap.com/place/B0FFFABCD",
        )
        d = r.to_dict()
        self.assertEqual(d["name"], "楼外楼")
        self.assertEqual(d["cuisine"], "江浙菜")
        self.assertEqual(d["rating"], 4.5)
        self.assertEqual(d["price_per_person"], 188.0)

    def test_search_restaurants_uses_nearby_endpoint_for_anchor(self):
        import amap_service

        poi = {
            "id": "B0TEST",
            "name": "附近餐厅",
            "address": "西湖区测试路",
            "location": "120.151,30.271",
            "type": "餐饮服务;中餐厅;杭帮菜",
            "business": {"rating": "4.7", "cost": "88", "business_area": "西湖"},
        }
        with patch("amap_service._get", return_value={"pois": [poi]}) as get:
            found = amap_service.search_restaurants(
                "杭州", location="120.15,30.27", radius=6000, limit=5
            )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].poi_detail_url, "https://www.amap.com/place/B0TEST")
        self.assertEqual(get.call_args.args[0], "/v5/place/around")
        self.assertEqual(get.call_args.kwargs["location"], "120.15,30.27")
        self.assertEqual(get.call_args.kwargs["radius"], 5000)

    def test_lunch_search_excludes_drinks_and_pastries_by_classification(self):
        categories = ["冷饮店", "饮品店", "奶茶店", "咖啡厅", "果汁店", "甜品店", "甜点店",
                      "冰淇淋店", "糕饼店", "糕点店", "蛋糕店", "面包店", "烘焙店", "茶艺馆"]
        pois = [{"id": f"B0BAD{i}", "name": "1点点(九溪烟树店)" if i == 0 else f"饮品糕点{i}",
                 "location": "120.151,30.271", "type": f"餐饮服务;{category}"}
                for i, category in enumerate(categories)]
        with patch("amap_service._get", return_value={"pois": pois}):
            self.assertEqual(search_restaurants("杭州", location="120.15,30.27", limit=25), [])

    def test_meal_classification_keeps_noodles_fast_food_and_tea_named_restaurants(self):
        entries = [("小吃面馆", "小吃店"), ("快餐饭店", "快餐厅"), ("面馆", "中餐厅"),
                   ("茶字中餐馆", "中餐厅;杭帮菜"), ("港式茶餐厅", "茶餐厅"),
                   ("啡尝卷·花园餐厅", "中餐厅")]
        pois = [{"id": f"B0GOOD{i}", "name": name, "location": "120.151,30.271", "type": f"餐饮服务;{category}"}
                for i, (name, category) in enumerate(entries)]
        with patch("amap_service._get", return_value={"pois": pois}):
            found = search_restaurants("杭州", location="120.15,30.27", limit=25)
        self.assertEqual({item.name for item in found}, {name for name, _ in entries})


class TestFetchFoodFallback(unittest.TestCase):
    """验证 tools._fetch_food 在高德失败时回退到 Tavily。"""

    def test_fallback_on_error(self):
        try:
            import tools  # noqa: F401
        except ImportError:
            self.skipTest("tools.py 依赖未安装（需要 Python >= 3.10 和 mcp/langchain）")
        with patch("tools.search_restaurants", side_effect=RuntimeError("amap error")):
            with patch("tools._fetch_web_search", return_value=[]):
                with patch("tools._extract_item_features", side_effect=lambda items, prompt: items):
                    import tools
                    result = tools._fetch_food("杭州", max_results=5)
                    self.assertIsInstance(result, list)

    def test_nearby_search_expands_radius_from_plan_anchor(self):
        try:
            import tools
        except ImportError:
            self.skipTest("tools.py 依赖未安装（需要 Python >= 3.10 和 mcp/langchain）")
        def fetch(_city, _keyword, _price, max_results, location, radius, allow_web_fallback):
            self.assertEqual(location, "120.15,30.27")
            self.assertFalse(allow_web_fallback)
            if radius == 1500:
                return []
            return [
                {
                    "name": f"餐厅{radius}-{i}",
                    "poi_id": f"{radius}-{i}",
                    "longitude": 120.151,
                    "latitude": 30.271,
                }
                for i in range(2)
            ]

        with patch("tools._fetch_food", side_effect=fetch) as mocked, \
             patch("tools._read_cache", return_value=None), patch("tools._write_cache"):
            result = tools.run_food_search({
                "destination": "杭州",
                "anchors": [{"day": 1, "meal": "午餐", "longitude": 120.15, "latitude": 30.27}],
            })
        self.assertEqual([call.kwargs["radius"] for call in mocked.call_args_list], [1500, 3000, 5000])
        options = result["food_by_anchor"][0]["restaurants"]
        self.assertEqual(len(options), 4)
        self.assertEqual(result["food_by_anchor"][0]["meal"], "午餐")

    def test_nearby_does_not_fall_back_to_citywide_web_results(self):
        import tools
        with patch("tools.search_restaurants", side_effect=RuntimeError("unavailable")), \
             patch("tools._fetch_web_search") as web:
            result = tools._fetch_food("杭州", location="120.15,30.27")
        self.assertEqual(result, [])
        web.assert_not_called()

    def test_invalid_anchor_is_not_searched(self):
        import tools
        with patch("tools._fetch_food") as fetch:
            result = tools.run_food_search({
                "destination": "杭州",
                "anchors": [{"day": 1, "meal": "午餐"}, {"longitude": 0, "latitude": 0}],
            })
        self.assertEqual(result["food_by_anchor"], [])
        fetch.assert_not_called()

    def test_food_budget_uses_actual_days_and_people(self):
        import tools
        budget = tools._estimate_food_budget({
            "total_budget": 3600, "travelers": "2人", "days": 3,
        })
        self.assertEqual(budget, 50)

    def test_first_search_preloads_food_for_frontend_display(self):
        import tools
        food_items = [{"name": "餐厅1"}]
        with patch("tools._read_cache", return_value=None), patch("tools._write_cache"), \
             patch("tools._fetch_weather", return_value={}), \
             patch("tools._fetch_hotels", return_value=[]), \
             patch("tools._fetch_poi_distributed", return_value=[]), \
             patch("tools._fetch_events", return_value=[]), \
             patch("tools._fetch_social_food", return_value=[]), \
             patch("tools._fetch_food", return_value=food_items) as food:
            result = tools.run_search({"destination": "杭州", "start_date": "2026-10-10"})
        food.assert_called_once()
        self.assertEqual(result["food"], food_items)
        self.assertTrue(result["food_search_pending"])

    def test_repeated_anchors_are_deduplicated_and_searched_concurrently(self):
        import tools
        barrier = threading.Barrier(3)
        def fetch(_city, _keyword, _price, **kwargs):
            barrier.wait(timeout=2)
            return [{"poi_id": f"{kwargs['location']}-{i}", "name": f"餐厅{i}"} for i in range(6)]
        anchors = [
            {"day": 1, "meal": "午餐", "longitude": 120.15, "latitude": 30.27},
            {"day": 1, "meal": "晚餐", "longitude": 120.16, "latitude": 30.28},
            {"day": 2, "meal": "午餐", "longitude": 120.15, "latitude": 30.27},
            {"day": 2, "meal": "晚餐", "longitude": 120.17, "latitude": 30.29},
        ]
        with patch("tools._fetch_food", side_effect=fetch) as mocked, \
             patch("tools._read_cache", return_value=None), patch("tools._write_cache"):
            result = tools.run_food_search({"destination": "杭州", "anchors": anchors})
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual([item["day"] for item in result["food_by_anchor"]], [1, 1, 2, 2])
        self.assertEqual(result["food_by_anchor"][0]["restaurants"], result["food_by_anchor"][2]["restaurants"])

    def test_cuisine_keyword_falls_back_to_same_nearby_radius(self):
        import tools
        nearby = [{"poi_id": f"{i}", "name": f"真实餐厅{i}", "cuisine": "江浙菜"} for i in range(6)]
        with patch("tools._fetch_food", side_effect=[[], nearby]) as fetch, \
             patch("tools._read_cache", return_value=None), patch("tools._write_cache"):
            result = tools.run_food_search({
                "destination": "杭州", "basic": {"food_keyword": "杭帮菜"},
                "anchors": [{"longitude": 120.15, "latitude": 30.27}],
            })
        self.assertEqual([call.args[1] for call in fetch.call_args_list], ["杭帮菜", None])
        self.assertEqual([call.kwargs["radius"] for call in fetch.call_args_list], [1500, 1500])
        self.assertTrue(all(call.kwargs["location"] == "120.15,30.27" for call in fetch.call_args_list))
        self.assertEqual(result["food_by_anchor"][0]["restaurants"][0]["cuisine"], "江浙菜")

    def test_nearby_meal_cache_uses_new_classification_version(self):
        import tools
        nearby = [{"poi_id": f"{i}", "name": f"真实餐厅{i}"} for i in range(6)]
        with patch("tools._cache_key", wraps=tools._cache_key) as key, \
             patch("tools._read_cache", return_value=None), patch("tools._write_cache"), \
             patch("tools._fetch_food", return_value=nearby):
            tools.run_food_search({"destination": "杭州", "anchors": [{"longitude": 120.15, "latitude": 30.27}]})
        self.assertEqual(key.call_args.args[1], "food-nearby-v2-meals")


class TestSearchOrchestrationLocal(unittest.TestCase):
    def test_search_cache_is_scoped_by_travel_style(self):
        import tools
        with patch("tools._read_cache", return_value=None) as cache, patch("tools._write_cache"), \
             patch("tools._fetch_weather", return_value={}), patch("tools._fetch_hotels", return_value=[]), \
             patch("tools._fetch_events", return_value=[]), patch("tools._fetch_social_food", return_value=[]), \
             patch("tools._fetch_poi_distributed", return_value=[]):
            base = {"destination": "杭州", "start_date": "2026-10-16", "end_date": "2026-10-17"}
            tools.run_search({**base, "profile": {"travel_style": ["自然景观"]}})
            tools.run_search({**base, "profile": {"travel_style": ["历史人文"]}})
        self.assertEqual(len({call.args[0] for call in cache.call_args_list}), 2)

    def test_actual_dates_and_basic_food_keyword_are_used(self):
        import tools
        with patch("tools._read_cache", return_value=None), patch("tools._write_cache"), \
             patch("tools._fetch_weather", return_value={}), patch("tools._fetch_hotels", return_value=[]), \
             patch("tools._fetch_events", return_value=[]), patch("tools._fetch_social_food", return_value=[]), \
             patch("tools._fetch_poi_distributed", return_value=[]), \
             patch("tools._estimate_food_budget", wraps=tools._estimate_food_budget) as budget:
            result = tools.run_search({
                "destination": "杭州", "start_date": "2026-10-16", "end_date": "2026-10-17",
                "basic": {"total_budget": 4000, "travelers": "2人", "food_keyword": "杭帮菜"},
            })
        self.assertEqual(result["food_keyword"], "杭帮菜")
        self.assertEqual(tools._estimate_food_budget(budget.call_args.args[0]), 83)

    def test_return_transport_remains_when_outbound_fails(self):
        import tools
        barrier = threading.Barrier(2)
        def fetch(origin, destination, day, **kwargs):
            barrier.wait(timeout=2)
            if origin == "上海":
                raise RuntimeError("去程不可用")
            return [{"train_no": "G123"}]
        result = tools._fetch_round_trip(fetch, "上海", "杭州", "2026-10-16", "2026-10-17")
        self.assertEqual(result, [{"train_no": "G123", "direction": "回"}])

    def test_day_trip_still_searches_return_transport(self):
        import tools
        with patch("tools._fetch_trains", return_value=[{"train_no": "G123"}]) as fetch:
            # 每次调用必须返回独立记录，避免 mock 的共享对象被 direction 改写。
            fetch.side_effect = lambda *_args, **_kwargs: [{"train_no": "G123"}]
            result = tools._fetch_round_trip(tools._fetch_trains, "上海", "杭州", "2026-10-16", "2026-10-16")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([item["direction"] for item in result], ["去", "回"])

    def test_day_trip_does_not_search_zero_night_hotels(self):
        import tools
        with patch("tools._read_cache", return_value=None), patch("tools._write_cache"), \
             patch("tools._fetch_weather", return_value={}), patch("tools._fetch_hotels") as hotels, \
             patch("tools._fetch_events", return_value=[]), patch("tools._fetch_social_food", return_value=[]), \
             patch("tools._fetch_poi_distributed", return_value=[]):
            result = tools.run_search({"destination": "杭州", "start_date": "2026-10-16"})
        hotels.assert_not_called()
        self.assertEqual(result["hotels"], [])

    def test_repeated_filtered_amap_pages_are_bounded(self):
        import amap_service
        coffee = {"name": "咖啡", "location": "120.15,30.27", "type": "餐饮服务;咖啡厅"}
        with patch("amap_service._get", return_value={"pois": [coffee] * 25}) as get:
            result = amap_service.search_restaurants("杭州", location="120.15,30.27", limit=25)
        self.assertEqual(result, [])
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
