import asyncio
import copy
import json
import os
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api.routes.plan import (
    PlanRequest,
    _blocks_to_plan,
    _merge_modified_blocks,
    _run_json,
    create_plan,
    plan_stream,
    city_guide_stream,
)


BLOCKS = [
    {"id": "a1", "plan_style": "经典", "day": 1, "date": "2026-10-20", "type": "景点", "time": "09:00-10:00", "name": "西湖", "link": "https://example.test/lake", "lng": 120.1, "lat": 30.2, "price": 0},
    {"id": "a2", "plan_style": "经典", "day": 1, "date": "2026-10-20", "type": "美食", "time": "12:00-13:00", "name": "午餐", "options": [{"name": "餐厅甲", "price": 70, "link": "https://example.test/food", "lng": 120.11, "lat": 30.21}], "lng": 120.11, "lat": 30.21, "price": 70},
    {"id": "a3", "plan_style": "经典", "day": 1, "date": "2026-10-20", "type": "酒店", "time": "住宿", "name": "酒店甲"},
    {"id": "b1", "plan_style": "轻松", "day": 1, "date": "2026-10-20", "type": "景点", "time": "10:00-11:00", "name": "西湖", "lng": 120.1, "lat": 30.2},
]


def request(modify, **extra):
    return PlanRequest(
        destination="杭州", start_date="2026-10-20", end_date="2026-10-21",
        basic={"total_budget": 5000}, search={"destination": "杭州", "poi": []},
        modify={"blocks": copy.deepcopy(BLOCKS), **modify}, **extra,
    )


def finalized(blocks, destination, start_date, end_date, *args, **kwargs):
    return {"blocks": blocks, "destination": destination, "start_date": start_date, "end_date": end_date,
        "legs": [{"from": blocks[0]["id"], "to": blocks[-1]["id"]}] if blocks else [], "total_cost": 70, "budget_status": "ok"}


class PlanMutationTests(unittest.TestCase):
    def test_rebuild_keeps_styles_options_links_coordinates_and_summaries(self):
        plan = _blocks_to_plan(BLOCKS, "杭州", "2026-10-20", "2026-10-21", {"summaries": {"经典": "文化行程"}})
        self.assertEqual([p["style"] for p in plan["plans"]], ["经典", "轻松"])
        self.assertEqual(plan["plans"][0]["summary"], "文化行程")
        meal = plan["plans"][0]["itinerary"][0]["schedule"][1]
        self.assertEqual(meal["options"], BLOCKS[1]["options"])
        self.assertEqual(meal["lng"], BLOCKS[1]["lng"])
        self.assertEqual(meal["id"], "a2")
        self.assertEqual(plan["plans"][0]["itinerary"][0]["date"], "2026-10-20")

    def test_merge_only_selected_id_and_drops_old_place_coordinates(self):
        change = {**BLOCKS[0], "name": "灵隐寺"}
        attempted_other_change = {**BLOCKS[-1], "name": "模型误改"}
        result = _merge_modified_blocks({"blocks": [change, attempted_other_change], "extra": "kept"}, BLOCKS, {"a1"})
        self.assertEqual(result["blocks"][-1], BLOCKS[-1])
        self.assertNotIn("lng", result["blocks"][0])
        self.assertNotIn("link", result["blocks"][0])
        self.assertEqual(result["extra"], "kept")

    def test_selected_modify_skips_classification_and_returns_finalized_routes(self):
        payload = request({"block_ids": ["a1"], "plan_style": "经典", "instruction": "换成灵隐寺"})
        changed = {**BLOCKS[0], "name": "灵隐寺", "lng": 120.09, "lat": 30.25, "link": "https://example.test/temple"}
        with patch("app.api.routes.plan._local_modify", return_value={"blocks": [changed]}) as local, patch("app.api.routes.plan._classify_modify") as classify, patch("app.api.routes.plan._search_context") as search, patch("app.api.routes.plan._finalize_blocks", side_effect=finalized) as finalize:
            result = create_plan(payload)
        classify.assert_not_called()
        search.assert_not_called()
        self.assertEqual(local.call_args.args[3][0]["id"], "a1")
        self.assertEqual(local.call_args.kwargs["full_blocks"], BLOCKS)
        self.assertEqual(result["blocks"][-1], BLOCKS[-1])
        self.assertEqual(result["blocks"][0]["link"], "https://example.test/temple")
        self.assertTrue(result["legs"])
        self.assertTrue(finalize.call_args.kwargs["refresh_food"])
        self.assertEqual(finalize.call_args.kwargs["refresh_targets"], [{"plan_style": "经典", "day": 1}])

    def test_do_not_go_too_far_modifies_food_instead_of_deleting_it(self):
        payload = request({"block_ids": ["a2"], "instruction": "餐厅不要太远，人均便宜一些"})
        with patch("app.api.routes.plan._local_modify", return_value={"blocks": [BLOCKS[1]]}) as local, patch("app.api.routes.plan._finalize_blocks", side_effect=finalized):
            result = create_plan(payload)
        local.assert_called_once()
        self.assertEqual(len(result["blocks"]), len(BLOCKS))

    def test_add_candidate_to_selected_day_preserves_link_and_coordinates(self):
        payload = request({"action": "add", "plan_style": "经典", "day": 1,
            "item": {"name": "美术馆", "type": "景点", "link": "https://example.test/art", "lng": 120.15, "lat": 30.23, "price": 20}})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized) as finalize:
            result = create_plan(payload)
        added = next(b for b in result["blocks"] if b["name"] == "美术馆")
        self.assertEqual(added["plan_style"], "经典")
        self.assertEqual(added["time"], "10:00-11:00")
        self.assertEqual(added["date"], "2026-10-20")
        self.assertEqual(added["link"], "https://example.test/art")
        self.assertEqual(added["lng"], 120.15)
        self.assertTrue(finalize.call_args.kwargs["refresh_food"])
        self.assertEqual(len({b["id"] for b in result["blocks"]}), len(result["blocks"]))

    def test_adding_restaurant_fills_existing_meal_without_duplicate_time_slot(self):
        payload = request({"action": "add", "plan_style": "经典", "day": 1, "block_ids": ["a2"],
            "item": {"name": "餐厅丙", "type": "美食", "link": "https://example.test/food-c", "lng": 120.12, "lat": 30.22, "price": 80}})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized) as finalize:
            result = create_plan(payload)
        meal = next(b for b in result["blocks"] if b["id"] == "a2")
        self.assertEqual(len(result["blocks"]), len(BLOCKS))
        self.assertEqual(meal["name"], "餐厅丙")
        self.assertEqual(meal["time"], "12:00-13:00")
        chosen = next(option for option in meal["options"] if option["name"] == meal["selected_option"])
        self.assertEqual(chosen["link"], "https://example.test/food-c")
        self.assertTrue(meal["user_selected"])
        self.assertFalse(finalize.call_args.kwargs["refresh_food"])

    def test_adding_hotel_replaces_same_day_accommodation(self):
        payload = request({"action": "add", "plan_style": "经典", "day": 1,
            "item": {"name": "酒店乙", "type": "酒店", "link": "https://example.test/hotel-b", "price": 400}})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized):
            result = create_plan(payload)
        hotels = [b for b in result["blocks"] if b["type"] == "酒店"]
        self.assertEqual(len(hotels), 1)
        self.assertEqual(hotels[0]["id"], "a3")
        self.assertEqual(hotels[0]["name"], "酒店乙")
        self.assertEqual(hotels[0]["time"], "住宿")

    def test_add_to_plan_after_user_deleted_every_block(self):
        payload = PlanRequest(destination="杭州", start_date="2026-10-20", end_date="2026-10-21", search={},
            modify={"action": "add", "blocks": [], "plan_style": "经典", "day": 2, "item": {"type": "景点", "name": "植物园"}})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized):
            result = create_plan(payload)
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["blocks"][0]["day"], 2)
        self.assertEqual(result["blocks"][0]["plan_style"], "经典")

    def test_explicit_food_choice_is_retained_without_automatic_research(self):
        candidate = {"name": "餐厅乙", "type": "美食", "link": "https://example.test/food-b", "lng": 120.12, "lat": 30.22, "price": 90, "options": BLOCKS[1]["options"], "selected_option": 1}
        payload = request({"action": "update", "block_ids": ["a2"], "item": candidate})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized) as finalize:
            result = create_plan(payload)
        selected = next(b for b in result["blocks"] if b["id"] == "a2")
        self.assertEqual(selected["name"], candidate["name"])
        self.assertEqual(selected["link"], candidate["link"])
        self.assertEqual(selected["options"], candidate["options"])
        self.assertTrue(selected["user_selected"])
        self.assertFalse(finalize.call_args.kwargs["refresh_food"])

    def test_delete_exact_id_preserves_same_name_in_other_style(self):
        payload = request({"action": "delete", "block_ids": ["a1"], "plan_style": "经典"})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized) as finalize:
            result = create_plan(payload)
        self.assertNotIn("a1", [b["id"] for b in result["blocks"]])
        self.assertIn("b1", [b["id"] for b in result["blocks"]])
        self.assertEqual(finalize.call_args.kwargs["refresh_targets"], [{"plan_style": "经典", "day": 1}])

    def test_global_modify_only_active_style_while_preserving_other_style(self):
        payload = request({"mode": "global", "plan_style": "经典", "instruction": "节奏轻松一些"})
        changed = [{**BLOCKS[0], "id": "b1", "name": "植物园"}, BLOCKS[2]]
        with patch("app.api.routes.plan._modify_plan", return_value={"blocks": changed}) as modify, patch("app.api.routes.plan._finalize_blocks", side_effect=finalized):
            result = create_plan(payload)
        self.assertTrue(all(b["plan_style"] == "经典" for b in modify.call_args.args[3]["blocks"]))
        other = next(b for b in result["blocks"] if b["plan_style"] == "轻松")
        self.assertEqual(other, BLOCKS[-1])
        self.assertEqual(len({b["id"] for b in result["blocks"]}), len(result["blocks"]))

    def test_legacy_named_targets_respect_day_and_style(self):
        payload = request({"mode": "block", "plan_style": "经典", "targets": [{"name": "西湖", "type": "景点", "day": 1}], "instruction": "取消这个景点"})
        with patch("app.api.routes.plan._finalize_blocks", side_effect=finalized):
            result = create_plan(payload)
        self.assertNotIn("a1", [b["id"] for b in result["blocks"]])
        self.assertIn("b1", [b["id"] for b in result["blocks"]])

    def test_stale_selected_ids_are_rejected_before_any_subprocess(self):
        payload = request({"block_ids": ["stale"], "instruction": "替换这里"})
        with patch("app.api.routes.plan.subprocess.run") as run:
            with self.assertRaises(HTTPException) as raised:
                create_plan(payload)
        self.assertEqual(raised.exception.status_code, 422)
        run.assert_not_called()

    def test_invalid_date_order_and_add_day_are_rejected(self):
        with self.assertRaises(HTTPException):
            create_plan(PlanRequest(destination="杭州", start_date="2026-10-21", end_date="2026-10-20"))
        with self.assertRaises(HTTPException):
            create_plan(request({"action": "add", "day": 4, "item": {"name": "美术馆"}}))

    def test_nonzero_agent_exit_does_not_return_success_json(self):
        with patch("app.api.routes.plan.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout=json.dumps({"blocks": BLOCKS}))):
            result = _run_json(SimpleNamespace(), SimpleNamespace(), {})
        self.assertIn("error", result)


class StreamCancellationTests(unittest.IsolatedAsyncioTestCase):
    """流式接口改为线程读取 subprocess.Popen（跨平台，不依赖事件循环支持子进程）。"""

    async def test_closing_stream_terminates_subprocess(self):
        payload = [b'{"type":"progress"}\n', b""]

        def fake_readline():
            return payload.pop(0) if payload else b""

        proc = MagicMock()
        proc.pid = 43210
        proc.returncode = None
        proc.poll.return_value = None  # 关闭时仍在运行，触发清理
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline.side_effect = fake_readline
        proc.stderr = MagicMock()
        proc.stderr.readline.return_value = b""

        def fake_wait(timeout=None):
            proc.returncode = 0
            return 0

        proc.wait.side_effect = fake_wait

        response = plan_stream(PlanRequest(destination="杭州", start_date="2026-10-20", end_date="2026-10-21"))
        with patch("app.api.routes.plan.subprocess.Popen", return_value=proc) as launch, \
                patch("app.api.routes.plan._stop_process_group", new=AsyncMock()) as stop:
            first = await response.body_iterator.__anext__()
            self.assertIn('"progress"', first)
            await response.body_iterator.aclose()

        self.assertTrue(launch.called)
        # 仅在 POSIX 上传 start_new_session；Windows 无进程组概念
        if os.name == "posix":
            self.assertTrue(launch.call_args.kwargs.get("start_new_session"))
        # 关闭流时必须回收子进程
        stop.assert_awaited()


if __name__ == "__main__":
    unittest.main()
