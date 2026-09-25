"""Draft 行程生成 Agent。"""

from typing import cast

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _normalise_draft_cards, _parse_json_response, get_llm
from app.agent.state import ItineraryCard, TravelAgentState, TravelAgentUpdate


def itinerary_generation_agent(state: TravelAgentState) -> TravelAgentUpdate:
    system_prompt = """你是行程规划专家。根据提供的旅行上下文生成 Draft v1 行程卡片。
严格遵守以下规则：
1. 只返回 JSON 数组，不要 Markdown 代码块，不要解释文字。
2. 根据每天的实际行程细节生成足够的卡片，不要为了固定数量合并或省略活动。
3. 只能使用输入上下文支持的信息；无法确认的字段使用 null，并在 status 写 estimated。
4. 必须覆盖 1 到行程天数的每日安排，不要生成额外日期。
每张卡片包含：day、type、title、time、duration、location、description、reason、cost、operating_hours、status
"""
    destination = state.get("selected_destination", "未知")
    raw_duration = state.get("travel_dates", {}).get("duration_days", 3)
    duration = max(1, min(int(raw_duration or 3), 7))
    reports = state.get("agent_reports", {})
    response = get_llm().invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=(
            f"旅行主题：{state.get('topic', '')}\n目的地：{destination}\n行程天数：{duration}天\n"
            f"出行时间：{state.get('travel_dates', {})}\n用户偏好：{state.get('user_preferences', {})}\n"
            f"预算：{state.get('budget', 0)}\n硬约束：{state.get('hard_constraints', [])}\n"
            f"时间与活动报告：{reports.get('time', {})}\n餐饮报告：{reports.get('dining', {})}\n"
            f"交通与住宿报告：{reports.get('transport', {})}"
        )),
    ])
    draft = _parse_json_response(_message_content(response))
    if not isinstance(draft, list):
        raise ValueError("行程生成 Agent 返回的不是 JSON 数组")
    if any(not isinstance(card, dict) or int(card.get("day", 0) or 0) < 1 or int(card.get("day", 0) or 0) > duration for card in draft):
        raise ValueError("行程生成 Agent 返回了超出行程天数范围的卡片")
    draft_cards_raw, card_dependencies = _normalise_draft_cards(draft)
    draft_cards = [cast(ItineraryCard, card) for card in draft_cards_raw]
    draft_itinerary = [dict(card) for card in draft_cards]
    return {
        "draft_itinerary": draft_itinerary, "draft_cards": draft_cards, "card_dependencies": card_dependencies,
        "agent_reports": {**state.get("agent_reports", {}), "generation": {
            "status": "complete", "summary": f"生成了 {len(draft)} 张行程卡片", "recommendations": draft_itinerary,
            "evidence_refs": [], "uncertainties": ["行程事实尚未经过外部 API 验证"], "cost_items": [], "question_candidates": [],
        }},
        "workflow_status": "completed", "current_stage": "generation", "next_action": "present_draft",
        "agent_response": "已为您生成 Draft v1 行程。",
    }
