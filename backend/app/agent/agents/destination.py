"""目的地探索 Agent。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _parse_json_response, get_llm
from app.agent.state import TravelAgentState, TravelAgentUpdate


def destination_discovery_agent(state: TravelAgentState) -> TravelAgentUpdate:
    response = get_llm().invoke([
        SystemMessage(content="你是目的地探索专家。根据用户需求推荐 3-5 个合适的目的地。每个目的地包含 name、reason、weather、style、estimated_cost。以 JSON 数组格式返回。"),
        HumanMessage(content=f"用户意图：{state.get('user_intent', '')}\n旅行主题：{state.get('topic', '')}\n已知目的地：{state.get('selected_destination') or '未明确'}"),
    ])
    candidates = _parse_json_response(_message_content(response))
    if not isinstance(candidates, list):
        raise ValueError("目的地探索 Agent 返回的不是 JSON 数组")
    return {"candidate_destinations": candidates,
            "agent_reports": {**state.get("agent_reports", {}), "destination": {"status": "complete", "summary": f"生成了 {len(candidates)} 个候选目的地", "recommendations": candidates, "evidence_refs": [], "uncertainties": ["天气和费用尚未接入实时 API"], "cost_items": [], "question_candidates": []}},
            "current_stage": "destination", "next_action": "wait_for_selection",
            "agent_response": f"为您找到 {len(candidates)} 个合适的目的地候选。"}