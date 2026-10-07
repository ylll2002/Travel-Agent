"""Supplemental search results survive planning and price-attachment boundaries."""
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import plan


class AuditSourceTests(unittest.TestCase):
    def test_supplemental_hotel_snapshot_is_returned_as_actual_source(self):
        search = {"destination": "杭州", "start_date": "2026-10-10", "end_date": "2026-10-11",
                  "hotels": [], "poi": [{"name": "西湖", "longitude": 120.1, "latitude": 30.2}]}
        extra = {"name": "补搜酒店", "price": 200, "longitude": 120.1, "latitude": 30.2,
                 "url": "https://example.test/hotel"}
        days = [{"day": d, "date": f"2026-10-{9+d}", "schedule": [
            {"type": "景点", "name": "西湖", "time": "09:00-11:00"}]} for d in (1, 2)]
        with patch("plan.OpenAI"), patch("plan._filter_far_pois", return_value=copy.deepcopy(search)), \
             patch("plan._select_transport", return_value={}), \
             patch("plan._assign_days_by_score", return_value=([{"day": 1, "names": ["西湖"]},
                    {"day": 2, "names": ["西湖"]}], {"西湖": 0})), \
             patch("plan._build_day_plan", side_effect=days), \
             patch("plan._enforce_day_schedule", side_effect=lambda day, *args: day), \
             patch("plan._search_hotels", return_value=[extra]), \
             patch("plan._dedupe_poi_names", side_effect=lambda result, sources: result), \
             patch("plan._reorder_by_proximity", side_effect=lambda result, city: result), \
             patch("plan.refresh_food_for_plan", side_effect=lambda result, *args, **kwargs: result) as refresh, \
             patch("plan._backfill_links", side_effect=lambda result, sources: result), \
             patch("plan.geocode", side_effect=AssertionError("No network")):
            result = plan.build_plan(search, basic={"budget_tiers": ["经济"]}, profile={"age_group": "60+"})
        self.assertEqual(refresh.call_args.kwargs["profile"], {"age_group": "60+"})
        self.assertEqual(result["suggestions"]["hotels"][0]["name"], extra["name"])
        self.assertEqual(result["source_updates"], {"hotels": [extra]})
        self.assertEqual(result["plans"][0]["itinerary"][0]["hotel"], extra["name"])

    def test_cli_price_attachment_uses_returned_supplemental_snapshot(self):
        extra = {"name": "补搜酒店", "price": 200, "url": "https://example.test/hotel"}
        generated = {"destination": "杭州", "start_date": "2026-10-10", "end_date": "2026-10-11", "days": 2,
            "plans": [{"style": "经典", "itinerary": [{"day": 1, "date": "2026-10-10", "hotel": "补搜酒店", "schedule": []}]}],
            "source_updates": {"hotels": [extra]}}
        payload = {"search": {"destination": "杭州", "hotels": []}, "basic": {"total_budget": 1000}}
        output = io.StringIO()
        with patch("plan.sys.stdin", io.StringIO(json.dumps(payload))), \
             patch("plan.build_plan", return_value=generated), \
             patch("plan.attach_routes"), patch("plan._align_route_times"), redirect_stdout(output):
            plan.main()
        result = json.loads(output.getvalue())
        hotel = next(b for b in result["blocks"] if b["type"] == "酒店")
        self.assertEqual(hotel["price"], 200)
        self.assertTrue(hotel["price_known"])
        self.assertEqual(result["source_updates"]["hotels"], [extra])

    def test_low_level_finalizer_never_carries_previous_review(self):
        previous = {"revision": 4, "blocks": [], "audit": {"passed": True},
                    "history": [{"passed": True}], "passed": True}
        with patch("plan.attach_routes"), patch("plan._align_route_times"):
            result = plan.finalize_plan(previous)
        self.assertNotIn("audit", result)
        self.assertNotIn("history", result)
        self.assertNotIn("passed", result)
        self.assertIn("audit", previous)

    def test_global_cli_finalizer_uses_updated_budget_instead_of_previous_input(self):
        changed = {"basic": {"total_budget": 200}, "plans": []}
        payload = {"plan": {"revision": 4}, "modify": {"mode": "global", "instruction": "预算改为200"},
                   "basic": {"total_budget": 100}}
        with patch("plan.sys.stdin", io.StringIO(json.dumps(payload))), \
             patch("plan.modify_plan", return_value=changed), patch("plan.blockify", return_value=[]), \
             patch("plan.finalize_plan", return_value={}) as finalize, redirect_stdout(io.StringIO()):
            plan.main()
        self.assertEqual(finalize.call_args.args[2], {"total_budget": 200})


if __name__ == "__main__":
    unittest.main()
