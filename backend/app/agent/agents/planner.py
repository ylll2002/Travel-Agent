"""负责阶段判断和路由的 Planner Agent。"""

from app.agent.state import TravelAgentState, TravelAgentUpdate


def _needs_dining_report(state: TravelAgentState) -> bool:
    preferences = state.get("user_preferences", {})
    constraints = state.get("hard_constraints", [])
    dining_keys = {"food", "cuisine", "dining", "diet", "allergy", "spicy"}
    return bool(
        any(key.lower() in dining_keys for key in preferences)
        or any("餐" in str(item).lower() or "食" in str(item).lower() for item in constraints)
        or state.get("user_action") == "request_dining"
    )


def _needs_transport_report(state: TravelAgentState) -> bool:
    preferences = state.get("user_preferences", {})
    topic = state.get("topic", "")
    constraints = " ".join(str(item) for item in state.get("hard_constraints", []))
    terms = ("交通", "步行", "地铁", "公交", "住宿", "酒店", "路线", "机场", "移动")
    return bool(
        any(key in {"mobility", "transport", "walking", "accommodation", "budget"} for key in preferences)
        or any(term in f"{topic} {constraints}" for term in terms)
        or state.get("route_requests")
        or state.get("user_action") == "request_transport"
    )


def planning_orchestrator_agent(state: TravelAgentState) -> TravelAgentUpdate:
    clarification_round = state.get("clarification_round", 0)
    questions_asked = state.get("questions_asked", 0)
    can_ask = clarification_round < 3 and questions_asked < 5
    if not state.get("user_intent"):
        decision, status, question = "understanding", "running", ""
    elif not state.get("candidate_destinations") and not state.get("selected_destination"):
        decision, status, question = "destination", "running", ""
    elif not state.get("selected_destination"):
        decision = "ask_user" if can_ask else "complete"
        status = "waiting_for_user" if can_ask else "completed"
        question = "请从候选目的地中选择一个，或告诉我你想去的其他地区。" if can_ask else ""
    elif not state.get("travel_dates") and not state.get("time_candidates"):
        decision, status, question = "time", "running", ""
    elif not state.get("travel_dates"):
        decision = "ask_user" if can_ask else "complete"
        status = "waiting_for_user" if can_ask else "completed"
        candidates = state.get("agent_reports", {}).get("time", {}).get("question_candidates", [])
        question = candidates[0] if candidates and can_ask else ("请选择一个出行时间方案，或补充你的出行日期和天数。" if can_ask else "")
    elif (("dining" not in state.get("agent_reports", {}) and _needs_dining_report(state))
          or ("transport" not in state.get("agent_reports", {}) and _needs_transport_report(state))):
        decision, status, question = "specialists", "running", ""
    elif not state.get("draft_cards") and not state.get("draft_itinerary"):
        decision, status, question = "generation", "running", ""
    else:
        decision, status, question = "complete", "completed", ""
    update: TravelAgentUpdate = {
        "next_action": decision,
        "workflow_status": status,
        "pending_question": question,
        "planning_round": state.get("planning_round", 0) + 1,
        "current_stage": "planning",
    }
    if decision == "ask_user":
        update["clarification_round"] = clarification_round + 1
        update["questions_asked"] = questions_asked + 1
    return update


def planner_route(state: TravelAgentState) -> str:
    return state.get("next_action", "complete")


def should_continue_planning(state: TravelAgentState) -> str:
    if state.get("current_stage") == "destination":
        return "time" if state.get("selected_destination") else "end"
    return state.get("next_action", "complete")