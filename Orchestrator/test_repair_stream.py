"""Real graph, stubbed subprocesses: per-round events and bounded safe repair."""
import copy
import unittest
from unittest.mock import patch
import orchestrator as flow
from shared.audit import normalize_audit


def block(time="09:00-11:00"):
    return {"id":"v1","plan_style":"经典","day":1,"date":"2026-10-10",
            "name":"博物馆","type":"景点","time":time,"price":30,"price_known":True}


def issue():
    return {"severity":"high","type":"时间","detail":"与午餐时间重叠","suggestion":"提前结束游览",
            "actionable":True,"plan_style":"经典","day":1,"block_ids":["v1"],
            "evidence":[{"path":"plan.blocks[0].time","value":"09:00-11:00"}]}


class RepairStreamTests(unittest.TestCase):
    def run_flow(self, audits, fail=False, repairing=False):
        calls=[]
        queue=list(audits)
        original={"revision":4,"destination":"杭州","start_date":"2026-10-10","end_date":"2026-10-10",
                  "blocks":[block()],"basic":{"total_budget":100}}
        data={"destination":"杭州","start_date":"2026-10-10","end_date":"2026-10-10",
              "basic":{"total_budget":100},"search":{"poi":[{"name":"博物馆"}]}}
        if repairing:
            data.update(plan=copy.deepcopy(original),iteration=1,repair_count=0,history=[])
        def call(python, script, payload):
            calls.append((script,copy.deepcopy(payload)))
            if script==flow.SEARCH_PY: return data["search"]
            if script==flow.PLAN_PY:
                if fail and payload.get("audit_repair"): return {"error":"test timeout"}
                return {"destination":"杭州","start_date":"2026-10-10","end_date":"2026-10-10",
                        "blocks":[block()]}
            if script==flow.VALIDATE_PY: return normalize_audit(queue.pop(0),payload["plan"])
            raise AssertionError(script)
        with patch.object(flow,"_call",side_effect=call):
            graph=flow.build_graph(repair_existing=repairing)
            events=list(flow.stream_events(graph,data,{"configurable":{"thread_id":"repair-stream-test"}}))
        return events,calls,original

    def test_two_repairs_stream_reasons_and_each_snapshot_before_pass(self):
        bad={"passed":False,"issues":[issue()]}
        events,calls,_=self.run_flow([bad,bad,{"passed":True,"issues":[]}])
        stages=[e.get("stage") for e in events if e.get("stage")]
        self.assertEqual(stages,["planning","reviewing","repairing","reviewing","repairing","reviewing","passed"])
        failures=[e for e in events if e.get("stage")=="repairing"]
        self.assertEqual([e["audit"]["plan_revision"] for e in failures],[1,2])
        self.assertEqual(failures[0]["audit"]["issues"][0]["detail"],"与午餐时间重叠")
        fixes=[p for s,p in calls if s==flow.PLAN_PY and p.get("audit_repair")]
        self.assertEqual(len(fixes),2)
        self.assertEqual([p["plan"]["revision"] for p in fixes],[1,2])
        self.assertTrue(all("与午餐时间重叠" in p["feedback"] for p in fixes))
        final=events[-1]["data"]
        self.assertTrue(final["audit"]["passed"])
        self.assertEqual(final["plan"]["revision"],3)
        self.assertEqual(final["workflow"],{"repair_count":2,"repair_limit":2,"stop_reason":"passed"})

    def test_repair_existing_rechecks_without_search_and_keeps_constraints(self):
        events,calls,original=self.run_flow([{"passed":False,"issues":[issue()]},{"passed":True,"issues":[]}],repairing=True)
        self.assertNotIn(flow.SEARCH_PY,[s for s,_ in calls])
        self.assertEqual([s for s,_ in calls],[flow.VALIDATE_PY,flow.PLAN_PY,flow.VALIDATE_PY])
        payload=next(p for s,p in calls if s==flow.PLAN_PY)
        self.assertEqual(payload["plan"],original)
        self.assertEqual(payload["basic"],original["basic"])
        self.assertEqual(events[-1]["data"]["plan"]["revision"],5)

    def test_limit_keeps_failed_verdict_and_full_history(self):
        bad={"passed":False,"issues":[issue()]}
        events,calls,_=self.run_flow([bad,bad,bad])
        final=events[-1]["data"]
        self.assertEqual(final["workflow"]["stop_reason"],"limit")
        self.assertEqual(final["workflow"]["repair_count"],2)
        self.assertFalse(final["audit"]["passed"])
        self.assertEqual(len(final["history"]),3)
        self.assertEqual(len([s for s,_ in calls if s==flow.PLAN_PY]),3)

    def test_failed_repair_keeps_original_revision_and_issues(self):
        events,calls,original=self.run_flow([{"passed":False,"issues":[issue()]}],fail=True,repairing=True)
        final=events[-1]["data"]
        self.assertEqual(final["plan"]["blocks"],original["blocks"])
        self.assertEqual(final["plan"]["revision"],original["revision"])
        self.assertEqual(final["audit"]["status"],"error")
        self.assertEqual(final["audit"]["issues"][0]["detail"],"与午餐时间重叠")
        self.assertEqual(len([s for s,_ in calls if s==flow.PLAN_PY]),1)

    def test_nonblocking_suggestions_and_service_errors_never_replan(self):
        for raw in ({"passed":True,"issues":[{**issue(),"severity":"medium","actionable":False}]},
                    {"passed":False,"issues":[],"error":"模型回复被截断"}):
            with self.subTest(raw=raw):
                events,calls,_=self.run_flow([raw],repairing=True)
                self.assertEqual([s for s,_ in calls],[flow.VALIDATE_PY])
                self.assertEqual(events[-1]["data"]["plan"]["revision"],4)
