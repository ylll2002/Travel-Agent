"""HTTP repair streams recheck snapshots and clean up children on cancellation."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from app.api.routes import plan as routes


class RepairRequestTests(unittest.TestCase):
    def request(self):
        return routes.PlanRequest(plan={"revision":4,"destination":"杭州","start_date":"2026-10-10","end_date":"2026-10-10",
          "basic":{"total_budget":100},"review_context":{"preferences":{"pace":"轻松"}},
          "audit":{"passed":True,"issues":[]},"history":[{"passed":True}],
          "blocks":[{"id":"v1","name":"博物馆","type":"景点","day":1,"date":"2026-10-10","plan_style":"经典"}]},
          search={"poi":[{"name":"博物馆"}]})

    def test_repair_keeps_sources_constraints_and_version_and_discards_client_verdict(self):
        payload=self.request()
        original=copy.deepcopy(payload.plan)
        with patch.object(routes,"_orchestrator_stream",return_value="stream") as stream:
            result=routes.repair_stream(payload)
        self.assertEqual(result,"stream")
        data=stream.call_args.args[0]
        self.assertEqual(data["plan"]["revision"],4)
        self.assertEqual(data["plan"]["blocks"],original["blocks"])
        self.assertNotIn("audit",data["plan"])
        self.assertNotIn("history",data["plan"])
        self.assertEqual(data["basic"],{"total_budget":100})
        self.assertEqual(data["search"],payload.search)
        self.assertEqual(data["preferences"],{"pace":"轻松"})
        self.assertTrue(stream.call_args.kwargs["repair"])
        self.assertEqual(payload.plan,original)

    def test_hidden_changes_and_invalid_versions_are_rejected(self):
        for changes in ({"basic":{"total_budget":1}},{"destination":"上海"},{"start_date":"2026-10-11"},
                        {"modify":{"instruction":"偷偷改日期"}}):
            with self.subTest(changes=changes),patch.object(routes,"_orchestrator_stream") as stream:
                payload=self.request()
                for key,value in changes.items(): setattr(payload,key,value)
                with self.assertRaises(HTTPException): routes.repair_stream(payload)
                stream.assert_not_called()
        for revision in (True,0,None,4.5):
            payload=self.request()
            payload.plan["revision"]=revision
            with self.subTest(revision=revision),self.assertRaises(HTTPException): routes.repair_stream(payload)


class RepairSSETests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_rounds_in_order_and_launches_repair_mode(self):
        events=[{"type":"node","stage":"reviewing"},{"type":"node","stage":"repairing","audit":{"passed":False}},
                {"type":"node","stage":"passed"},{"type":"final","data":{"passed":True}}]
        proc=SimpleNamespace(pid=43210,returncode=0,stdin=MagicMock(),stdout=MagicMock(),stderr=MagicMock(),wait=AsyncMock())
        proc.stdin.drain=AsyncMock()
        proc.stdout.readline=AsyncMock(side_effect=[json.dumps(e).encode()+b"\n" for e in events]+[b""])
        proc.stderr.read=AsyncMock(return_value=b"")
        response=routes.repair_stream(RepairRequestTests().request())
        with patch.object(routes.asyncio,"create_subprocess_exec",new=AsyncMock(return_value=proc)) as launch,              patch.object(routes,"_stop_process_group",new=AsyncMock()) as cleanup:
            chunks=[chunk async for chunk in response.body_iterator]
        self.assertIn("--repair",launch.call_args.args)
        self.assertEqual([json.loads(c[6:].strip()) for c in chunks[:-1]],events)
        self.assertIn("[DONE]",chunks[-1])
        cleanup.assert_awaited_once_with(proc)

    async def test_cancel_repairs_stops_child_process_group(self):
        proc=SimpleNamespace(pid=43210,returncode=None,stdin=MagicMock(),stdout=MagicMock(),stderr=MagicMock(),wait=AsyncMock())
        proc.stdin.drain=AsyncMock()
        proc.stdout.readline=AsyncMock(return_value=b'{"type":"node","stage":"reviewing"}\n')
        proc.stderr.read=AsyncMock(return_value=b"")
        response=routes.repair_stream(RepairRequestTests().request())
        with patch.object(routes.asyncio,"create_subprocess_exec",new=AsyncMock(return_value=proc)),              patch.object(routes,"_stop_process_group",new=AsyncMock()) as cleanup:
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
        cleanup.assert_awaited_once_with(proc)
