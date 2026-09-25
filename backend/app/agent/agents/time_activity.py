"""出行时间与活动规划 Agent。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _parse_json_response, get_llm
from app.agent.state import TravelAgentState, TravelAgentUpdate


def time_activity_planning_agent(state: TravelAgentState) -> TravelAgentUpdate:
    response = get_llm().invoke([
        SystemMessage(content="你是旅行时间与活动规划专家。根据目的地和用户需求，给出 2-3 个出行时间候选。返回 JSON：{\"candidates\":[{\"label\":\"...\",\"start_date\":\"YYYY-MM-DD\",\"end_date\":\"YYYY-MM-DD\",\"duration_days\":5,\"reason\":\"...\",\"activities\":[\"...\"]}],\"question\":\"...\"}。所有判断必须标记为估计。不要直接替用户选择唯一日期。"),
        HumanMessage(content=f"目的地：{state.get('selected_destination', '未知')}\n需求：{state.get('topic', '')}"),
    ])
    parsed = _parse_json_response(_message_content(response))
    candidates = parsed.get("candidates", [])
    return {"time_candidates": candidates,
            "agent_reports": {**state.get("agent_reports", {}), "time": {"status": "estimated", "summary": f"已生成 {len(candidates)} 个时间候选，等待用户选择", "recommendations": candidates, "evidence_refs": [], "uncertainties": ["天气、节日和活动信息为 LLM 估计，尚未接入实时 API"], "cost_items": [], "question_candidates": [parsed.get("question", "请确认出行日期和天数")]}},
            "current_stage": "time", "workflow_status": "waiting_for_user", "pending_question": parsed.get("question", "请选择一个出行时间方案。"), "agent_response": "我根据当前信息整理了几个出行时间候选。"}