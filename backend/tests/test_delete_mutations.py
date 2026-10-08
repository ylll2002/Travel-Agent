"""Delete explicit user-selected stops without replacement, replanning or live APIs."""

import copy
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.api.routes import plan as plan_routes
from app.api.routes.plan import PLAN_PY, PLAN_PYTHON, VALIDATE_PY, PlanRequest, create_plan
from shared.audit import combine_audits
from ValidateAgent.rules import validate_rules


SCULPTURE_NAME = "中国雕塑院·青岛雕塑园"
BLOCKS = [
    {"id": "sculpture", "plan_style": "经典", "day": 1, "date": "2026-10-20",
     "type": "景点", "name": SCULPTURE_NAME, "time": "09:00-11:00",
     "lng": 120.46, "lat": 36.08, "price": 20},
    {"id": "lunch", "plan_style": "经典", "day": 1, "date": "2026-10-20",
     "type": "美食", "name": "已选午餐店", "meal": "午餐", "time": "12:00-13:00",
     "lng": 120.461, "lat": 36.081, "price": 60, "user_selected": True,
     "selected_option": "已选午餐店", "link": "https://example.test/lunch",
     "options": [{"name": "已选午餐店", "price": 60, "lng": 120.461, "lat": 36.081,
                  "link": "https://example.test/lunch"}]},
    {"id": "square", "plan_style": "经典", "day": 1, "date": "2026-10-20",
     "type": "景点", "name": "五四广场", "time": "14:00-16:00",
     "lng": 120.38, "lat": 36.06, "price": 30},
    {"id": "alternative", "plan_style": "轻松", "day": 1, "date": "2026-10-20",
     "type": "景点", "name": "八大关", "time": "10:00-12:00",
     "lng": 120.35, "lat": 36.05, "price": 80},
]


@unittest.skipUnless(Path(PLAN_PYTHON).exists(), "PlanAgent local environment is unavailable")
class DeleteMutationIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Empty keys prevent dotenv from loading any real credentials. The only
        # subprocess allowed here is the deterministic PlanAgent finalizer.
        env = {**os.environ, "OPENAI_API_KEY": "", "AMAP_KEY": ""}
        env.pop("__PYVENV_LAUNCHER__", None)
        self.env_patch = patch("app.api.routes.plan._subprocess_env", return_value=env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.finalizer_inputs = []
        original_runner = plan_routes._run_json

        def offline_runner(python, script, data, *args, **kwargs):
            if script == VALIDATE_PY:
                rule = validate_rules(data["plan"], data["search"], data["basic"])
                model = {"passed": True, "issues": []} if rule["passed"] else None
                return combine_audits(rule, model, data["plan"])
            self.assertEqual(script, PLAN_PY)
            self.assertTrue(data.get("finalize"), "Deletion must not call a planning model")
            self.assertFalse(data.get("refresh_food"), "Deletion must preserve remaining meal choices")
            self.finalizer_inputs.append(copy.deepcopy(data))
            return original_runner(python, script, data, *args, **kwargs)

        runner_patch = patch("app.api.routes.plan._run_json", side_effect=offline_runner)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        self.model_calls = []
        for name in ("_classify_modify", "_local_modify", "_modify_plan", "_search_context"):
            guard = patch(f"app.api.routes.plan.{name}", side_effect=AssertionError(f"Deletion called {name}"))
            self.model_calls.append(guard.start())
            self.addCleanup(guard.stop)

    def make_request(self, modification, blocks=None):
        source = copy.deepcopy(BLOCKS if blocks is None else blocks)
        metadata = {
            "blocks": copy.deepcopy(source),
            "revision": 1,
            "summaries": {"经典": "经典路线", "轻松": "悠闲路线"},
            "food": [{"name": "右侧原餐厅候选", "day": 1, "plan_style": "经典", "price_per_person": 60}],
            "legs": [{"plan_style": "经典", "day": 1, "from": "sculpture", "to": "lunch",
                      "from_id": "sculpture", "to_id": "lunch",
                      "polyline": [[120.46, 36.08], [120.461, 36.081]]}],
        }
        return PlanRequest(
            destination="青岛", start_date="2026-10-20", end_date="2026-10-21",
            basic={"travelers": "1人", "total_budget": 1000},
            search={"destination": "青岛", "poi": []}, plan=metadata,
            modify={"blocks": copy.deepcopy(source), **modification},
        )

    def assert_deleted(self, result, ids, names):
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(result["mutation"], {
            "action": "delete", "removed_block_ids": ids, "removed_names": names,
        })
        self.assertTrue(set(ids).isdisjoint(block["id"] for block in result["blocks"]))
        for plan in result.get("plans", []):
            for day in plan.get("itinerary", []):
                self.assertTrue(set(ids).isdisjoint(item.get("id") for item in day.get("schedule", [])))
        for leg in result.get("legs", []):
            self.assertTrue(set(ids).isdisjoint(
                leg.get(field) for field in ("from", "to", "from_id", "to_id")))
        for call in self.model_calls:
            call.assert_not_called()

    def test_partial_name_delete_reaches_real_finalizer_and_preserves_other_choices(self):
        payload = self.make_request({"instruction": "把雕塑园删了", "plan_style": "经典"})
        original = payload.model_dump()
        result = create_plan(payload)
        self.assert_deleted(result, ["sculpture"], [SCULPTURE_NAME])
        self.assertEqual([block["id"] for block in result["blocks"]], ["lunch", "square", "alternative"])
        self.assertEqual(result["cost_by_style"], {"经典": 90, "轻松": 80})
        self.assertEqual(result["plans"][1]["summary"], "悠闲路线")
        self.assertEqual(result["food"], original["plan"]["food"])
        meal = next(block for block in result["blocks"] if block["id"] == "lunch")
        for field in ("name", "time", "link", "selected_option", "user_selected", "lng", "lat"):
            self.assertEqual(meal[field], BLOCKS[1][field])
        self.assertEqual([option["name"] for option in meal["options"]], ["已选午餐店"])
        self.assertEqual(payload.model_dump(), original, "Deletion must not mutate the submitted snapshot")

    def test_common_spoken_delete_instructions_do_not_invoke_a_model(self):
        for instruction in ("删掉雕塑园", "删除雕塑园", "把雕塑园去掉", "雕塑园不去了", "不要去雕塑园",
                            "请把雕塑园删除吧", "删除雕塑园，不用换成别的"):
            with self.subTest(instruction=instruction):
                result = create_plan(self.make_request({"instruction": instruction, "plan_style": "经典"}))
                self.assert_deleted(result, ["sculpture"], [SCULPTURE_NAME])

    def test_selected_block_ids_have_priority_over_a_different_name_in_text(self):
        result = create_plan(self.make_request({
            "instruction": "把雕塑园删了", "plan_style": "经典", "block_ids": ["square"],
        }))
        self.assert_deleted(result, ["square"], ["五四广场"])
        self.assertIn("sculpture", [block["id"] for block in result["blocks"]])

    def test_explicit_delete_action_can_delete_multiple_selected_blocks(self):
        result = create_plan(self.make_request({
            "action": "delete", "plan_style": "经典", "block_ids": ["sculpture", "square"],
        }))
        self.assert_deleted(result, ["sculpture", "square"], [SCULPTURE_NAME, "五四广场"])
        self.assertEqual(result["cost_by_style"], {"经典": 60, "轻松": 80})

    def test_deleting_last_stop_does_not_replace_it_or_require_a_remaining_candidate(self):
        for modification in ({"instruction": "雕塑园不去了"}, {"action": "delete", "block_ids": ["sculpture"]}):
            with self.subTest(modification=modification):
                result = create_plan(self.make_request(modification, blocks=[BLOCKS[0]]))
                self.assert_deleted(result, ["sculpture"], [SCULPTURE_NAME])
                self.assertEqual(result["blocks"], [])
                self.assertEqual(result.get("legs", []), [])
                self.assertEqual(result["cost_by_style"], {})
                self.assertEqual(result["total_cost"], 0)
                self.assertFalse(any(day.get("schedule") for plan in result.get("plans", [])
                                     for day in plan.get("itinerary", [])))

    def test_day_and_style_disambiguate_repeated_names(self):
        repeated = [
            BLOCKS[0],
            {**BLOCKS[0], "id": "day2-sculpture", "day": 2, "date": "2026-10-21"},
            {**BLOCKS[0], "id": "other-style-sculpture", "plan_style": "轻松"},
        ]
        result = create_plan(self.make_request({
            "instruction": "把雕塑园删了", "plan_style": "经典", "day": 2,
        }, blocks=repeated))
        self.assert_deleted(result, ["day2-sculpture"], [SCULPTURE_NAME])
        self.assertEqual({block["id"] for block in result["blocks"]}, {"sculpture", "other-style-sculpture"})

    def test_repeated_name_without_day_requires_selection(self):
        repeated = [BLOCKS[0], {**BLOCKS[0], "id": "day2-sculpture", "day": 2}]
        with self.assertRaises(HTTPException) as raised:
            create_plan(self.make_request({"instruction": "删掉雕塑园", "plan_style": "经典"}, blocks=repeated))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "ambiguous_delete")
        self.assertEqual([candidate["id"] for candidate in raised.exception.detail["candidates"]],
                         ["sculpture", "day2-sculpture"])
        self.assertIn("选择", raised.exception.detail["message"])
        self.assertEqual(self.finalizer_inputs, [])

    def test_partial_name_matching_multiple_distinct_places_requires_selection(self):
        repeated = [BLOCKS[0], {**BLOCKS[2], "id": "other-sculpture", "name": "海滨雕塑园"}]
        with self.assertRaises(HTTPException) as raised:
            create_plan(self.make_request({"instruction": "把雕塑园删了", "plan_style": "经典"}, blocks=repeated))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "ambiguous_delete")
        self.assertEqual([candidate["name"] for candidate in raised.exception.detail["candidates"]],
                         [SCULPTURE_NAME, "海滨雕塑园"])
        self.assertEqual(self.finalizer_inputs, [])

    def test_unmatched_delete_name_does_not_replan_or_delete_another_stop(self):
        with self.assertRaises(HTTPException) as raised:
            create_plan(self.make_request({"instruction": "删掉不存在的公园", "plan_style": "经典"}))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self.finalizer_inputs, [])

    def test_stale_selected_id_does_not_fall_back_to_instruction_name(self):
        with self.assertRaises(HTTPException) as raised:
            create_plan(self.make_request({
                "instruction": "把雕塑园删了", "plan_style": "经典", "block_ids": ["stale-id"],
            }))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("失效", raised.exception.detail)
        self.assertEqual(self.finalizer_inputs, [])

    def test_replacement_and_negative_delete_requests_do_not_use_delete_path(self):
        for instruction in ("把雕塑园换成石老人", "删掉雕塑园换成石老人", "别删雕塑园", "不要删除雕塑园"):
            with self.subTest(instruction=instruction):
                sentinel = {"error": "non-delete model path intentionally stopped"}
                with patch("app.api.routes.plan._local_modify", return_value=sentinel) as local:
                    result = create_plan(self.make_request({
                        "instruction": instruction, "plan_style": "经典", "block_ids": ["sculpture"],
                    }))
                self.assertEqual(result, sentinel)
                local.assert_called_once()
                self.assertEqual(self.finalizer_inputs, [])


if __name__ == "__main__":
    unittest.main()
