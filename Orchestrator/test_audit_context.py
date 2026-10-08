import copy
import unittest

from orchestrator import _plan_for_validate, _search_for_validate


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
        self.assertNotIn("options", result["blocks"][0])
        self.assertNotIn("food_by_anchor", result)
        self.assertEqual(plan, original)

    def test_review_search_keeps_weather_and_selected_source(self):
        search = {"weather": {"days": [{"weather": "雨"}]},
                  "food": [{"name": "选中餐厅", "cuisine": "杭帮菜", "price_per_person": 80},
                           {"name": "其他餐厅"}], "social_food": [{"text": "长文"}]}
        result = _search_for_validate(search, {"blocks": [{"name": "选中餐厅"}]})
        self.assertEqual(result["weather"], search["weather"])
        self.assertEqual(result["food"], search["food"][:1])
        self.assertNotIn("social_food", result)


if __name__ == "__main__":
    unittest.main()
