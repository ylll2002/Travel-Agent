"""餐饮选择 Agent。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _parse_json_response, get_llm
from app.agent.state import SpecialistReport, TravelAgentState, TravelAgentUpdate


def dining_selection_agent(state: TravelAgentState) -> TravelAgentUpdate:
    response = get_llm().invoke([
        SystemMessage(content="你是餐饮规划专家。返回 JSON 数组，每项包含 name、area、reason、price_per_person、opening_hours、estimated_cost、uncertainty。没有 API 时只能给出 LLM 估计，并明确 uncertainty。"),
        HumanMessage(content=f"目的地：{state.get('selected_destination', '未知')}\n偏好：{state.get('user_preferences', {})}"),
    ])
    recommendations = _parse_json_response(_message_content(response))
    report: SpecialistReport = {
        "status": "estimated", "summary": "已准备当地餐饮方向", "recommendations": recommendations,
        "evidence_refs": [], "uncertainties": ["餐厅、营业时间和价格为 LLM 估计，尚未接入实时 API"],
        "cost_items": [{"amount": item.get("estimated_cost")} for item in recommendations if isinstance(item, dict)],
        "question_candidates": [],
    }
    return {"specialist_results": [{"agent": "dining", "report": report}], "current_stage": "dining", "agent_response": "已为您挑选当地特色餐厅。"}
