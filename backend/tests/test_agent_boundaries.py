from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.interaction import apply_revision_decision, apply_user_selection
from app.agent.session_store import InMemorySessionStore, PersistentSessionStore
from app.agent.state import InitialTravelAgentState, TravelAgentState
from app.agent.state_factory import create_initial_state
from app.agent.travel_agent import TravelAgent
from app.agent.workflow_runner import WorkflowRunner, merge_update
from app.api.routes import agent as agent_route
from app.db import Base
from app.main import app
from app.schemas import ChatMessage, ChatRequest, ChatResponse


def initial_state(
    *,
    messages: list[dict],
    topic: str,
    session_id: str,
    user_action: str = "start_planning",
) -> InitialTravelAgentState:
    return cast(
        InitialTravelAgentState,
        create_initial_state(
            messages=messages,
            topic=topic,
            session_id=session_id,
            user_action=user_action,
        ),
    )


def complete_state(state: TravelAgentState) -> InitialTravelAgentState:
    return cast(InitialTravelAgentState, state)


def test_initial_state_factory_sets_complete_defaults():
    state = initial_state(
        messages=[{"role": "user", "content": "去东京"}],
        topic="去东京",
        session_id="session-1",
    )

    assert state["session_id"] == "session-1"
    assert state["trip_brief"] == {"topic": "去东京", "granularity_level": "topic"}
    assert state["workflow_status"] == "running"
    assert state["candidate_destinations"] == []
    assert state["hard_constraints"] == []


def test_chat_response_validates_agent_payload_and_preserves_extra_fields():
    response = ChatResponse.model_validate({
        "reply": "已生成行程",
        "draft_cards": [{
            "card_id": "card-1",
            "day": 1,
            "type": "activity",
            "title": "浅草寺",
            "status": "estimate",
            "provider_note": "来自候选结果",
        }],
        "validation_results": {
            "revision": {
                "status": "passed",
                "message": "校验通过",
            },
        },
        "revision_comparison": [{
            "card_id": "card-1",
            "before": {
                "card_id": "card-1",
                "day": 1,
                "type": "activity",
                "title": "公园",
            },
        }],
    })

    assert response.draft_cards[0].card_id == "card-1"
    assert (response.draft_cards[0].model_extra or {})["provider_note"] == "来自候选结果"
    assert response.validation_results["revision"].status == "passed"
    assert response.revision_comparison[0].before.title == "公园"


def test_travel_agent_chat_runs_new_planning_and_persists_result(monkeypatch):
    store = InMemorySessionStore()
    travel_agent = TravelAgent(session_store=store)
    planned_state = initial_state(
        messages=[],
        topic="去东京",
        session_id="session-1",
    )
    planned_state["agent_response"] = "已准备东京方案"

    monkeypatch.setattr(
        travel_agent,
        "_run_graph_with_trace",
        lambda _graph, _state: planned_state,
    )

    reply, session_id, result = travel_agent.chat(ChatRequest(
        messages=[ChatMessage(role="user", content="去东京")],
        session_id="session-1",
    ))

    assert reply == "已准备东京方案"
    assert session_id == "session-1"
    assert result is planned_state
    assert store.get("session-1") is planned_state


def test_travel_agent_chat_rejects_modification_without_existing_plan():
    travel_agent = TravelAgent(session_store=InMemorySessionStore())

    reply, session_id, result = travel_agent.chat(ChatRequest(
        messages=[ChatMessage(role="user", content="修改第一天")],
        session_id="session-2",
    ))

    assert session_id == "session-2"
    assert complete_state(result)["workflow_status"] == "waiting_for_user"
    assert "没有可修改" in reply


def test_travel_agent_chat_rejects_revision_without_pending_result():
    travel_agent = TravelAgent(session_store=InMemorySessionStore())

    reply, session_id, result = travel_agent.chat(ChatRequest(
        messages=[ChatMessage(role="user", content="接受修改")],
        session_id="session-3",
    ))

    assert session_id == "session-3"
    assert complete_state(result)["workflow_status"] == "waiting_for_user"
    assert "没有待确认" in reply


def test_agent_chat_returns_structured_nested_response(monkeypatch):
    class _FakeAgent:
        def chat(self, _request):
            return (
                "已生成行程",
                "session-1",
                {
                    "workflow_status": "completed",
                    "time_candidates": [{"label": "下个月", "duration_days": 4}],
                    "draft_cards": [{
                        "card_id": "card-1",
                        "day": 1,
                        "type": "activity",
                        "title": "浅草寺",
                    }],
                    "evidence_refs": [{"evidence_id": "evidence-1", "source": "guide"}],
                    "validation_results": {
                        "schedule": {"status": "passed", "warnings": []},
                    },
                    "revision_comparison": [{
                        "card_id": "card-1",
                        "before": {
                            "card_id": "card-1",
                            "day": 1,
                            "type": "activity",
                            "title": "公园",
                        },
                    }],
                },
            )

    monkeypatch.setattr(agent_route, "agent", _FakeAgent())

    with TestClient(app) as client:
        response = client.post("/api/agent/chat", json={
            "messages": [{"role": "user", "content": "去东京"}],
        })

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "session-1"
    assert payload["time_candidates"][0]["label"] == "下个月"
    assert payload["draft_cards"][0]["title"] == "浅草寺"
    assert payload["validation_results"]["schedule"]["status"] == "passed"
    assert payload["revision_comparison"][0]["before"]["title"] == "公园"


def test_user_selection_updates_brief_and_clears_destination_candidates():
    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")
    state["candidate_destinations"] = [{"name": "东京", "reason": "交通方便"}]

    apply_user_selection(state, "东京")

    assert state["selected_destination"] == "东京"
    assert state["candidate_destinations"] == []
    assert cast(dict, state["trip_brief"])["destination"] == "东京"
    assert state["user_action"] == "select_destination"


def test_user_selection_updates_dates_and_clears_time_candidates():
    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")
    state["time_candidates"] = [{"label": "下个月", "duration_days": 4}]

    apply_user_selection(state, "下个月")

    assert state["time_candidates"] == []
    assert state["travel_dates"]["source"] == "user_selected_estimate"
    assert cast(dict, state["trip_brief"])["duration_days"] == 4
    assert state["user_action"] == "select_time"


def test_revision_decision_can_restore_before_cards():
    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")
    state["draft_cards"] = [{"card_id": "card-1", "day": 1, "title": "博物馆"}]
    state["revision_comparison"] = [{
        "card_id": "card-1",
        "before": {"card_id": "card-1", "day": 1, "title": "公园"},
    }]

    result = complete_state(apply_revision_decision(state, "保留旧版本"))

    assert result["current_itinerary"] == [{"card_id": "card-1", "day": 1, "title": "公园"}]
    assert result["revision_comparison"] == []
    assert result["user_action"] == "confirm_revision"


def test_merge_update_appends_messages_and_replaces_other_fields():
    state = initial_state(
        messages=[{"role": "user", "content": "开始"}],
        topic="城市旅行",
        session_id="session-1",
    )

    merge_update(cast(TravelAgentState, state), {
        "messages": [{"role": "assistant", "content": "好的"}],
        "workflow_status": "waiting_for_user",
    })

    assert len(state["messages"]) == 2
    assert state["messages"][-1]["content"] == "好的"
    assert state["workflow_status"] == "waiting_for_user"


class _FakeGraph:
    def __init__(self, updates):
        self.updates = updates

    def stream(self, _state, stream_mode):
        assert stream_mode == "updates"
        yield from self.updates


def test_workflow_runner_records_success_events_and_trace():
    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")
    graph = _FakeGraph([
        {"planner": {"workflow_status": "waiting_for_user"}},
        {"understanding": {"messages": [{"role": "assistant", "content": "请补充时间"}]}},
    ])
    runner = WorkflowRunner()

    result = runner.run(graph, state)
    _, trace = runner.stream_trace(graph, state)

    result = complete_state(result)
    assert result["workflow_status"] == "waiting_for_user"
    assert result["workflow_events"][-1]["event"] == "workflow_completed"
    assert [item["node"] for item in trace] == ["planner", "understanding"]
    assert trace[-1]["next_node"] == "__end__"


def test_workflow_runner_converts_graph_error_to_error_state():
    class _FailingGraph:
        def stream(self, _state, stream_mode):
            raise RuntimeError("测试错误")
            yield stream_mode

    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")
    result = WorkflowRunner().run(_FailingGraph(), state)

    result = complete_state(result)
    assert result["workflow_status"] == "error"
    assert "测试错误" in result["agent_response"]
    assert result["workflow_events"][-1]["event"] == "workflow_failed"


def test_in_memory_session_store_round_trip_and_clear():
    store = InMemorySessionStore()
    state = initial_state(messages=[], topic="城市旅行", session_id="session-1")

    store.save("session-1", state)

    assert store.get("session-1") is state
    store.clear()
    assert store.get("session-1") is None


def test_persistent_session_store_survives_new_store_instance(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    state = create_initial_state(messages=[], topic="城市旅行", session_id="session-1")

    PersistentSessionStore(session_factory).save("session-1", state)
    restored = PersistentSessionStore(session_factory).get("session-1")

    assert restored == state
    PersistentSessionStore(session_factory).delete("session-1")
    assert PersistentSessionStore(session_factory).get("session-1") is None
    engine.dispose()
