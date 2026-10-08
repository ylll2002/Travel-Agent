"""Contract checks use stubs, without model keys or search requests."""
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import replan


def client():
    app = FastAPI()
    app.include_router(replan.router, prefix="/api")
    return TestClient(app)


def payload():
    return {"plan": {"destination": "杭州", "start_date": "2026-10-20", "revision": 0, "blocks": [{"id": "a", "plan_style": "推荐方案", "type": "景点"}]}, "revision": 0, "plan_style": "推荐方案", "target_block_ids": ["a"], "instruction": "换个室内景点"}


def test_success(monkeypatch):
    calls = []
    def run(module, script, data, timeout):
        calls.append((module, data))
        return {"poi": []} if module == "SearchAgent" else {"revision": 1, "plan": {"blocks": [], "legs": []}, "changes": []}
    monkeypatch.setattr(replan, "run_module", run)
    response = client().post("/api/plan/replan", json=payload())
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert [c[0] for c in calls] == ["SearchAgent", "PlanAgent"]
    assert calls[1][1]["target_block_ids"] == ["a"]


def test_invalid_target_rejected_before_search(monkeypatch):
    monkeypatch.setattr(replan, "run_module", lambda *_: (_ for _ in ()).throw(AssertionError("must not call services")))
    data = payload()
    data["target_block_ids"] = ["missing"]
    assert client().post("/api/plan/replan", json=data).status_code == 422


def test_revision_conflict_before_search(monkeypatch):
    monkeypatch.setattr(replan, "run_module", lambda *_: (_ for _ in ()).throw(AssertionError("must not call services")))
    data = payload()
    data["revision"] = 1
    assert client().post("/api/plan/replan", json=data).status_code == 409


def test_whitespace_reason_rejected():
    data = payload()
    data["instruction"] = "   "
    assert client().post("/api/plan/replan", json=data).status_code == 422


def test_timeout_preserves_input(monkeypatch):
    def timeout(*_):
        raise subprocess.TimeoutExpired("agent", 180)
    monkeypatch.setattr(replan, "run_module", timeout)
    response = client().post("/api/plan/replan", json=payload())
    assert response.status_code == 504
    assert "原方案已保留" in response.json()["detail"]


def test_no_candidates_triggers_one_refresh(monkeypatch):
    calls = []
    def run(module, script, data, timeout):
        calls.append((module, data))
        if module == "SearchAgent":
            return {"poi": []}
        return {"error": "没有可用的同类替代项，请调整原因或重新搜索。"} if len(calls) == 2 else {"revision": 1, "plan": {"blocks": [], "legs": []}, "changes": []}
    monkeypatch.setattr(replan, "run_module", run)
    response = client().post("/api/plan/replan", json=payload())
    assert response.status_code == 200
    assert len(calls) == 4
    assert calls[2][1]["force_refresh"] is True
