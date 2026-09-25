from types import SimpleNamespace

from app.agent import common, specialists
from app.agent.nodes import (
    _needs_dining_report,
    _needs_transport_report,
    planning_orchestrator_agent,
)


def _ready_state(**updates):
    state = {
        "user_intent": "规划城市旅行",
        "candidate_destinations": [{"name": "东京"}],
        "selected_destination": "东京",
        "travel_dates": {"duration_days": 3},
        "time_candidates": [],
        "agent_reports": {},
        "draft_cards": [],
        "draft_itinerary": [],
        "user_preferences": {},
        "hard_constraints": [],
        "topic": "城市旅行",
        "route_requests": [],
        "user_action": "continue_planning",
    }
    state.update(updates)
    return state


def test_planner_skips_unneeded_specialists():
    result = planning_orchestrator_agent(_ready_state())

    assert result["next_action"] == "generation"
    assert _needs_dining_report(_ready_state()) is False
    assert _needs_transport_report(_ready_state()) is False


def test_planner_dispatches_specialists_for_explicit_preferences():
    state = _ready_state(
        user_preferences={"mobility": "walking", "cuisine": "spicy"},
    )

    result = planning_orchestrator_agent(state)

    assert result["next_action"] == "specialists"
    assert _needs_dining_report(state) is True
    assert _needs_transport_report(state) is True


def test_common_parses_fenced_json_and_structured_message_content():
    assert common._parse_json_response('```json\n{"ok": true}\n```') == {"ok": True}
    assert common._message_content(SimpleNamespace(content=[{"text": "东京"}])) == (
        '[{"text": "东京"}]'
    )


def test_normalise_draft_cards_adds_ids_and_same_day_dependencies():
    cards, dependencies = common._normalise_draft_cards([
        {"day": 1, "title": "浅草寺"},
        {"day": 1, "title": "上野公园", "dependency_ids": ["custom"]},
        {"day": 2, "title": "奈良公园"},
    ])

    assert [card["card_id"] for card in cards] == ["card-1", "card-2", "card-3"]
    assert cards[1]["dependency_ids"] == ["custom", "card-1"]
    assert dependencies == {
        "card-1": [],
        "card-2": ["custom", "card-1"],
        "card-3": [],
    }


def test_specialist_batch_merges_successful_reports(monkeypatch):
    monkeypatch.setattr(specialists, "dining_selection_agent", lambda _state: {
        "specialist_results": [{
            "agent": "dining",
            "report": {"status": "estimated", "summary": "餐饮"},
        }],
        "agent_response": "餐饮已完成",
    })
    monkeypatch.setattr(specialists, "transport_accommodation_agent", lambda _state: {
        "specialist_results": [{
            "agent": "transport",
            "report": {"status": "estimated", "summary": "交通"},
        }],
        "agent_response": "交通已完成",
    })

    result = specialists.run_specialist_batch(_ready_state(
        user_preferences={"cuisine": "spicy", "mobility": "walking"},
    ))

    assert result["agent_reports"] == {
        "dining": {"status": "estimated", "summary": "餐饮"},
        "transport": {"status": "estimated", "summary": "交通"},
    }
    assert result["agent_tasks"] == [
        {"agent": "dining", "status": "completed", "error": None},
        {"agent": "transport", "status": "completed", "error": None},
    ]
    assert result["agent_response"] == "餐饮已完成；交通已完成"


def test_specialist_batch_records_failed_agent_without_aborting(monkeypatch):
    def fail(_state):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(specialists, "dining_selection_agent", fail)

    result = specialists.run_specialist_batch(_ready_state(
        user_preferences={"cuisine": "spicy"},
    ))

    specialist_result = result["specialist_results"][0]
    assert specialist_result["status"] == "failed"
    assert specialist_result["error"] == "provider unavailable"
    assert result["agent_tasks"] == [{"agent": "dining", "status": "failed", "error": None}]