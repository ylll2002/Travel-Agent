"""Price regressions without SDK imports, paid requests, or map services.

The legacy planning functions are loaded from their actual AST so their price
logic can be exercised independently of plan.py's external SDK imports.
"""
import ast
import json
import math
import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from shared.pricing import item_price, parse_price, price_sort_key
from PlanAgent.replan import amount, replacement


def planning_functions():
    path = ROOT / "PlanAgent" / "plan.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_pick_hotel", "_pick_restaurant", "_pick_restaurants", "_attach_prices", "_valid_food_coord", "_positive_food_number", "_verified_walking_distance", "_coord_key"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    route_tree = ast.parse((ROOT / "PlanAgent" / "route_map.py").read_text(encoding="utf-8"))
    nodes += [node for node in route_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_km"]
    namespace = {"item_price": item_price, "parse_price": parse_price, "price_sort_key": price_sort_key, "math": math, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class PricingTests(unittest.TestCase):
    def test_supported_quotes(self):
        for raw, expected in [("¥1125", 1125), ("￥1,125.50", 1125.5), ("RMB 1,125", 1125), ("CNY 120元/人", 120), ("人民币100元", 100), ("１，１２５", 1125), (" 免费 ", 0), ("免票", 0), (0, 0), (12.5, 12.5), ("¥0", 0), ("100元起/晚", 100)]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_price(raw), expected)

    def test_unknown_and_ambiguous_quotes(self):
        for raw in [None, "", "暂无报价", "¥1**", "¥100-200", "100到200元", "$120", "12,34", "-10", -10, float("inf"), float("nan"), True, "酒店123价格未知"]:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_price(raw))

    def test_numeric_large_amount_is_finite(self):
        self.assertEqual(parse_price(1e20), 1e20)
        self.assertIsNone(parse_price(10 ** 10000))

    def test_item_fallback_and_explicit_free(self):
        self.assertEqual(item_price({"price": 0, "ticketPrice": 200}), 0)
        self.assertEqual(item_price({"price": "未知", "price_per_person": "¥80"}), 80)
        self.assertEqual(item_price({"free": True}), 0)
        self.assertIsNone(item_price({"price": "¥1**"}))

    def test_unknown_prices_sort_last_in_both_directions(self):
        rows = [None, "¥1125", "￥580", "暂无报价"]
        self.assertEqual(sorted(rows, key=price_sort_key)[:2], ["￥580", "¥1125"])
        self.assertEqual(sorted(rows, key=lambda p: price_sort_key(p, True))[:2], ["¥1125", "￥580"])

    def test_replan_amount_handles_commas_and_unknown(self):
        self.assertEqual(amount("¥1,125"), 1125)
        self.assertIsNone(amount("¥1**"))
        self.assertIsNone(amount("100-200"))

    def test_replacement_uses_same_parser(self):
        slot = {"id": "a", "type": "酒店"}
        self.assertEqual(replacement(slot, {"name": "酒店乙", "price": "¥1,125"})["price"], 1125)
        self.assertIsNone(replacement(slot, {"name": "酒店乙", "price": "¥1**"})["price"])


class PlanningPriceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = planning_functions()

    def test_original_hotel_error_and_economy_sort(self):
        hotels = [{"name": "上海东郊宾馆", "price": "¥1125"}, {"name": "便宜酒店", "price": "￥580"}, {"name": "未知酒店", "price": "暂无报价"}]
        result = self.functions["_pick_hotel"](hotels, None, set(), "经济")
        self.assertEqual(result["name"], "便宜酒店")
        self.assertEqual(self.functions["_pick_hotel"](hotels[:1], None, set(), "经济")["name"], "上海东郊宾馆")

    def test_hotel_luxury_unknown_not_first(self):
        hotels = [{"name": "未知", "price": None}, {"name": "甲", "price": "¥1,125"}, {"name": "乙", "price": "¥580"}]
        self.assertEqual(self.functions["_pick_hotel"](hotels, None, set(), "豪华")["name"], "甲")

    def test_restaurant_single_selection(self):
        food = [{"name": "未知", "price_per_person": None}, {"name": "甲", "price_per_person": "¥120"}, {"name": "乙", "price_per_person": "￥80"}]
        self.assertEqual(self.functions["_pick_restaurant"](food, "", set(), None, "经济")["name"], "乙")

    def test_weighted_restaurants_accept_currency_strings_without_old_tier_filter(self):
        food = [{"name": "经济", "price_per_person": "¥120"}, {"name": "舒适", "price_per_person": "￥350"}, {"name": "豪华", "price_per_person": "CNY 600"}]
        for item in food:
            item.update(longitude=120.001, latitude=30, rating=4.5)
        for tier in ("经济", "舒适", "豪华"):
            with self.subTest(tier=tier):
                rows = self.functions["_pick_restaurants"](food, "", set(), [120, 30], tier)
                self.assertEqual(rows[0]["name"], "经济")
                self.assertEqual({row["name"] for row in rows}, {"经济", "舒适", "豪华"})
                self.assertTrue(next(row for row in rows if row["name"] == "豪华")["over_meal_budget"])

    def test_restaurant_unknown_quote_is_not_treated_as_free(self):
        food = [{"name": "未知", "price_per_person": "暂无"}, {"name": "甲", "price_per_person": "¥900"}]
        for item in food:
            item.update(longitude=120.001, latitude=30, rating=4.5)
        rows = self.functions["_pick_restaurants"](food, "", set(), [120, 30], "经济")
        unknown = next(row for row in rows if row["name"] == "未知")
        self.assertIsNone(item_price(unknown))
        self.assertTrue(next(row for row in rows if row["name"] == "甲")["over_meal_budget"])

    def test_total_cost_and_budget_include_symbol_prices(self):
        plan = {"blocks": [{"id": "h", "type": "酒店", "name": "酒店"}, {"id": "f", "type": "美食", "name": "餐厅"}, {"id": "p", "type": "景点", "name": "免费景点"}]}
        search = {"hotels": [{"name": "酒店", "price": "¥1,125"}], "food": [{"name": "餐厅", "price_per_person": "￥80.50"}], "poi": [{"name": "免费景点", "free": True}]}
        result = self.functions["_attach_prices"](plan, search, "¥1,000")
        self.assertEqual(result["total_cost"], 1205.5)
        self.assertEqual(result["budget_status"], "over")
        self.assertEqual(result["blocks"][2]["price"], 0)

    def test_unknown_is_not_zero_or_budget_ok(self):
        result = self.functions["_attach_prices"]({"blocks": [{"id": "h", "type": "酒店", "name": "酒店"}]}, {"hotels": [{"name": "酒店", "price": "¥1**"}]}, 1000)
        self.assertIsNone(result["blocks"][0]["unit_price"])
        self.assertFalse(result["blocks"][0]["price_known"])
        self.assertEqual(result["unpriced_items"], {"推荐方案": ["酒店"]})
        self.assertEqual(result["budget_status"], "unknown")

    def test_exact_unknown_price_does_not_borrow_another_branch_price(self):
        result = self.functions["_attach_prices"](
            {"blocks": [{"id": "h", "type": "酒店", "name": "酒店"}]},
            {"hotels": [{"name": "酒店", "price": "未知"}, {"name": "酒店分店", "price": "¥500"}]},
        )
        self.assertIsNone(result["blocks"][0]["unit_price"])
        self.assertFalse(result["blocks"][0]["price_known"])

    def test_flight_and_train_prices(self):
        search = {"flights": {"outbound": [{"airline": "东航", "flight_no": "MU1", "price": "¥420"}]}, "trains": [{"transport": "火车", "train_no": "G1", "price": "￥200"}]}
        result = self.functions["_attach_prices"]({"blocks": [{"id": "f", "type": "交通", "name": "东航MU1"}, {"id": "t", "type": "交通", "name": "火车G1"}]}, search)
        self.assertEqual(result["total_cost"], 620)

    def test_no_stay_is_free(self):
        result = self.functions["_attach_prices"]({"blocks": [{"id": "h", "type": "酒店", "name": "当晚返程，无住宿"}]}, {})
        self.assertEqual(result["blocks"][0]["price"], 0)

    def test_real_cached_hotel_if_available(self):
        path = ROOT / "SearchAgent/cache/add82e7925f02b2b8a892260ab1eaa2c.json"
        if not path.exists():
            self.skipTest("Local search cache unavailable")
        hotels = json.loads(path.read_text(encoding="utf-8"))["hotels"]
        selected = self.functions["_pick_hotel"](hotels, None, set(), "经济")
        self.assertEqual(parse_price(selected["price"]), min(p for h in hotels if (p := parse_price(h.get("price"))) is not None))


if __name__ == "__main__":
    unittest.main()
