"""用户选择与局部修订确认的领域逻辑。"""

from typing import cast

from app.agent.state import InitialTravelAgentState, ItineraryCard, TravelAgentState, TripBrief

StateInput = InitialTravelAgentState | TravelAgentState


def apply_user_selection(state: StateInput, message: str) -> None:
    """将用户对候选目的地或时间方案的选择写回旅行摘要。"""
    candidate_destinations = cast(list[dict], state.get("candidate_destinations", []))
    selected = next(
        (item for item in candidate_destinations if item.get("name") == message.strip()),
        None,
    )
    brief = cast(TripBrief, dict(state.get("trip_brief", {})))
    if selected:
        destination = str(selected.get("name", ""))
        state["selected_destination"] = destination
        state["candidate_destinations"] = []
        brief["destination"] = destination
        brief["destination_scope"] = "selected_candidate"
        state["user_action"] = "select_destination"
        state["trip_brief"] = brief
        return

    time_candidates = cast(list[dict], state.get("time_candidates", []))
    selected_time = next(
        (item for item in time_candidates if item.get("label") == message.strip()),
        None,
    )
    if selected_time:
        travel_dates = {**selected_time, "source": "user_selected_estimate"}
        state["travel_dates"] = travel_dates
        brief["travel_dates"] = travel_dates
        brief["duration_days"] = travel_dates.get("duration_days", 0)
        state["time_candidates"] = []
        state["user_action"] = "select_time"
        state["trip_brief"] = brief


def apply_revision_decision(state: StateInput, decision: str) -> TravelAgentState:
    """确认或撤销当前状态中待确认的局部重规划结果。"""
    comparison = state.get("revision_comparison", [])
    if not comparison:
        state["workflow_status"] = "waiting_for_user"
        state["agent_response"] = "当前没有待确认的局部重规划结果。"
        return cast(TravelAgentState, state)

    if "保留旧版本" in decision or "撤销修改" in decision:
        cards = list(state.get("draft_cards") or state.get("draft_itinerary", []))
        before_by_id = {
            item.get("card_id"): item.get("before")
            for item in comparison
            if item.get("card_id") and item.get("before")
        }
        restored_cards: list[ItineraryCard] = []
        for card in cards:
            previous_card = before_by_id.get(card.get("card_id"))
            restored_cards.append(cast(ItineraryCard, previous_card or card))
        state["draft_cards"] = restored_cards
        state["draft_itinerary"] = [dict(card) for card in restored_cards]
        state["current_itinerary"] = [dict(card) for card in restored_cards]
        state["agent_response"] = "已保留原版本，局部修改已撤销。"
    else:
        accepted_cards = list(state.get("draft_cards") or state.get("draft_itinerary", []))
        state["current_itinerary"] = [dict(card) for card in accepted_cards]
        state["agent_response"] = "已采用局部重规划的新版本。"

    state["revision_comparison"] = []
    state["workflow_status"] = "waiting_for_user"
    state["user_action"] = "confirm_revision"
    return cast(TravelAgentState, state)
