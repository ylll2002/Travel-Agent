"""HTTP repair streams recheck snapshots and clean up children on cancellation."""
import copy
import json
import queue
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
from app.api.routes import plan as routes


def fake_popen(stdout_lines, block=False):
    """构造 subprocess.Popen 替身：stdout 是 line-buffered 的字节流。

    流式实现现在用线程读取 Popen 的管道，所以替身只需提供 readline()/poll()/
    terminate()/kill()/wait() 这些同步接口。block=True 时永不返回 EOF，
    用于验证"客户端断开即清理子进程"。
    """
    items = queue.Queue()
    for line in stdout_lines:
        items.put(line)
    if not block:
        items.put(b"")

    proc = MagicMock()
    proc.pid = 43210
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.readline.return_value = b""

    def readline():
        while True:
            try:
                return items.get_nowait()
            except queue.Empty:
                if block:
                    time.sleep(0.02)
                    continue
                return b""

    proc.stdout = MagicMock()
    proc.stdout.readline.side_effect = readline

    def wait(timeout=None):
        proc.returncode = 0
        return 0

    proc.wait.side_effect = wait
    proc.poll.side_effect = lambda: proc.returncode
    return proc


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
        # 流式实现改为线程读取 subprocess.Popen 的管道（Windows 的 --reload 下
        # Selector 事件循环不支持 asyncio 子进程），因此这里 mock Popen。
        proc=fake_popen([json.dumps(e).encode()+b"\n" for e in events])
        response=routes.repair_stream(RepairRequestTests().request())
        with patch.object(routes.subprocess,"Popen",return_value=proc) as launch, \
                patch.object(routes,"_stop_process_group",new=AsyncMock()) as cleanup:
            chunks=[chunk async for chunk in response.body_iterator]
        self.assertIn("--repair",launch.call_args.args[0])
        self.assertEqual([json.loads(c[6:].strip()) for c in chunks[:-1]],events)
        self.assertIn("[DONE]",chunks[-1])
        cleanup.assert_awaited_once_with(proc)

    async def test_cancel_repairs_stops_child_process_group(self):
        proc=fake_popen([b'{"type":"node","stage":"reviewing"}\n'],block=True)
        response=routes.repair_stream(RepairRequestTests().request())
        with patch.object(routes.subprocess,"Popen",return_value=proc), \
                patch.object(routes,"_stop_process_group",new=AsyncMock()) as cleanup:
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
        cleanup.assert_awaited_once_with(proc)
