"""交通与住宿规划 Agent。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _parse_json_response, get_llm
from app.agent.state import SpecialistReport, TravelAgentState, TravelAgentUpdate


def transport_accommodation_agent(state: TravelAgentState) -> TravelAgentUpdate:
    response = get_llm().invoke([
        SystemMessage(content="你是交通与住宿规划专家。返回 JSON：{\"recommendations\":[{\"area\":\"\",\"transport\":\"\",\"accommodation\":\"\",\"estimated_cost\":0,\"reason\":\"\"}]}。所有费用和可行性都是 LLM 估计，不能声称已查证。"),
        HumanMessage(content=f"目的地：{state.get('selected_destination', '未知')}\n行程：{state.get('travel_dates', {})}"),
    ])
    parsed = _parse_json_response(_message_content(response))
    recommendations = parsed.get("recommendations", [])
    report: SpecialistReport = {
        "status": "estimated", "summary": "已准备交通与住宿规划条件", "recommendations": recommendations,
        "evidence_refs": [], "uncertainties": ["路线、住宿和费用为 LLM 估计，尚未接入实时 API"],
        "cost_items": [{"amount": item.get("estimated_cost")} for item in recommendations], "question_candidates": [],
    }
    return {"specialist_results": [{"agent": "transport", "report": report}], "current_stage": "transport", "agent_response": "已规划交通和住宿安排。"}
