"""需求理解 Agent。"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.common import _message_content, _parse_json_response, get_llm
from app.agent.state import TravelAgentState, TravelAgentUpdate, merge_memory_into_trip_brief


def requirement_understanding_agent(state: TravelAgentState) -> TravelAgentUpdate:
    response = get_llm().invoke([
        SystemMessage(content="你是旅行需求理解专家。分析用户的旅行想法，提取用户意图、颗粒度(topic/region/city/time/details/ready)、缺失信息和偏好。以 JSON 格式返回：{\"user_intent\":\"...\",\"granularity_level\":\"...\",\"missing_info\":[...],\"preferences\":{}}"),
        HumanMessage(content=f"用户输入：{state.get('topic', '')}"),
    ])
    parsed = _parse_json_response(_message_content(response))
    brief, preferences, constraints = merge_memory_into_trip_brief(
        state.get("topic", ""), parsed.get("preferences", {}), parsed.get("hard_constraints", []),
        state.get("profile_memory", []), state.get("session_memory", []), state.get("candidate_memory", []),
    )
    brief["granularity_level"] = parsed.get("granularity_level", "topic")
    brief["missing_fields"] = parsed.get("missing_info", [])
    return {
        "user_intent": parsed.get("user_intent", ""), "granularity_level": parsed.get("granularity_level", "topic"),
        "missing_info": parsed.get("missing_info", []),
        "user_preferences": {str(item.get("key")): item.get("value") for item in preferences if item.get("key")},
        "hard_constraints": constraints, "trip_brief": brief,
        "session_memory": [item for item in preferences if item.get("source") == "current_session"],
        "confirmed_facts": {"user_intent": parsed.get("user_intent", ""), "granularity_level": parsed.get("granularity_level", "topic")},
        "current_stage": "understanding", "agent_response": "我理解了您的旅行想法，让我为您寻找合适的目的地。",
    }