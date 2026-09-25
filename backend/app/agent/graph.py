"""LangGraph 工作流构建。

这个文件是核心：它定义了 Agent 之间的协作流程。
把所有 Agent 节点组装成一个有向图（workflow）。
"""

from typing import Any, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agent.agents.destination import destination_discovery_agent
from app.agent.agents.dining import dining_selection_agent
from app.agent.agents.generation import itinerary_generation_agent
from app.agent.agents.planner import (
    planner_route,
    planning_orchestrator_agent,
)
from app.agent.agents.time_activity import time_activity_planning_agent
from app.agent.agents.transport import transport_accommodation_agent
from app.agent.agents.understanding import requirement_understanding_agent
from app.agent.common import _message_content, _normalise_draft_cards, _parse_json_response, get_llm
from app.agent.revision import validate_revision_cards
from app.agent.specialists import run_specialist_batch
from app.agent.state import ItineraryCard, TravelAgentState, TravelAgentUpdate

_validate_revision_cards = validate_revision_cards
_run_specialist_batch = run_specialist_batch


def create_travel_agent_graph():
    """创建 TripRadar 的 LangGraph 工作流。
    
    工作流程：
    1. Planner 根据状态选择需求理解、目的地和时间任务。
    2. 目的地确定后，时间任务按依赖顺序运行。
    3. 餐饮与交通住宿任务按需选择；互不依赖时由 specialists 批处理节点并行执行。
    4. 专业报告汇总后回到 Planner，再决定提问、继续研究或生成卡片。
    
    返回：编译好的可执行图
    """
    
    # 第一步：创建状态图
    # StateGraph 是 LangGraph 的核心类，管理整个工作流
    workflow = StateGraph(TravelAgentState)
    
    # 第二步：添加节点（每个 Agent）
    # 语法：add_node(节点名称, 节点函数)
    workflow.add_node("planner", planning_orchestrator_agent)
    workflow.add_node("understanding", requirement_understanding_agent)
    workflow.add_node("destination", destination_discovery_agent)
    workflow.add_node("time", time_activity_planning_agent)
    workflow.add_node("dining", dining_selection_agent)
    workflow.add_node("transport", transport_accommodation_agent)
    workflow.add_node("generation", itinerary_generation_agent)
    workflow.add_node("specialists", _run_specialist_batch)
    
    # 第三步：定义入口点
    # 对话开始时，先执行 understanding 节点
    workflow.set_entry_point("planner")
    
    # 第四步：添加边（定义节点间的连接）
    # add_edge(从节点, 到节点)：无条件连接
    workflow.add_edge("understanding", "planner")
    workflow.add_conditional_edges(
        "planner",
        planner_route,
        {
            "understanding": "understanding",
            "destination": "destination",
            "ask_user": END,
            "time": "time",
            "specialists": "specialists",
            "generation": "generation",
            "complete": END,
        }
    )
    workflow.add_edge("destination", "planner")
    workflow.add_edge("time", "planner")
    workflow.add_edge("specialists", "planner")
    workflow.add_edge("generation", "planner")
    
    # 第五步：编译图
    # 编译后的图可以被调用，LangGraph 会按照定义的顺序执行节点
    return workflow.compile()


def create_modification_graph():
    """创建修改行程的子图。
    
    用于处理用户修改单张卡片的场景：
    1. 分析修改请求
    2. 识别受影响的卡片
    3. 局部重规划
    4. 生成对比方案
    
    这是一个简化版本，实际应该包含影响分析节点。
    """
    workflow = StateGraph(TravelAgentState)
    
    # 添加修改相关的节点（简化）
    def analyze_modification(state: TravelAgentState) -> TravelAgentUpdate:
        """分析修改请求，识别影响范围。"""
        request = state.get("modification_request", "").lower()
        cards = state.get("draft_cards") or state.get("current_itinerary", [])
        dependencies = state.get("card_dependencies", {})
        direct_ids = {
            str(card.get("card_id"))
            for card in cards
            if card.get("card_id")
            and (
                str(card.get("card_id")).lower() in request
                or str(card.get("title", "")).lower() in request
                or str(card.get("type", "")).lower() in request
            )
        }
        affected_ids = set(direct_ids)
        changed = True
        while changed:
            changed = False
            for card_id, required_ids in dependencies.items():
                if any(required_id in affected_ids for required_id in required_ids) and card_id not in affected_ids:
                    affected_ids.add(card_id)
                    changed = True
        affected_indexes = [
            index for index, card in enumerate(cards)
            if card.get("card_id") in affected_ids
        ]
        if not affected_indexes:
            return {
                "affected_cards": [],
                "modification_scope": "unknown",
                "workflow_status": "waiting_for_user",
                "pending_question": "请指出要修改的卡片名称或卡片编号。",
                "agent_response": "我还无法确定这次修改会影响哪张卡片。",
            }
        return {
            "affected_cards": affected_indexes,
            "modification_scope": "local",
            "agent_response": "分析修改影响中..."
        }
    
    def regenerate_cards(state: TravelAgentState) -> TravelAgentUpdate:
        """只重规划受影响卡片，并把新版本合并回完整行程。"""
        cards = state.get("draft_cards") or state.get("current_itinerary", [])
        affected_indexes = set(state.get("affected_cards", []))
        affected_cards = [cards[index] for index in affected_indexes if index < len(cards)]
        try:
            response = get_llm().invoke([
                SystemMessage(content=(
                    "你是局部行程修改专家。只修改输入的受影响卡片，返回 JSON 数组。"
                    "每项必须保留原 card_id、day 和 type，并根据用户要求更新内容。"
                    "不要修改未提供的卡片，不要声称已验证外部事实。"
                )),
                HumanMessage(content=(
                    f"修改要求：{state.get('modification_request', '')}\n"
                    f"受影响卡片：{affected_cards}"
                )),
            ])
            parsed = _parse_json_response(_message_content(response))
            valid, validation_message = _validate_revision_cards(
                [cast(dict[str, Any], dict(card)) for card in affected_cards],
                parsed,
            )
            if not valid:
                raise ValueError(validation_message)
            revised_cards_raw, _ = _normalise_draft_cards(parsed)
            revised_cards = [cast(ItineraryCard, card) for card in revised_cards_raw]
            revised_by_id = {card.get("card_id"): card for card in revised_cards}
            merged_cards: list[ItineraryCard] = [
                cast(ItineraryCard, revised_by_id.get(card.get("card_id"), card))
                if index in affected_indexes
                else cast(ItineraryCard, card)
                for index, card in enumerate(cards)
            ]
            merged_cards_raw, card_dependencies = _normalise_draft_cards(
                [cast(dict[str, Any], dict(card)) for card in merged_cards]
            )
            merged_cards = [cast(ItineraryCard, card) for card in merged_cards_raw]
            comparison = [
                {
                    "card_id": old_card.get("card_id"),
                    "before": old_card,
                    "after": revised_by_id.get(old_card.get("card_id")),
                }
                for old_card in affected_cards
            ]
            return {
                "draft_itinerary": [dict(card) for card in merged_cards],
                "draft_cards": merged_cards,
                "card_dependencies": card_dependencies,
                "validation_results": {
                    "revision": {
                        "status": "passed",
                        "message": validation_message,
                    }
                },
                "revision_comparison": comparison,
                "workflow_status": "waiting_for_user",
                "agent_response": "局部重规划已完成，请查看新旧卡片对比。",
            }
        except Exception as error:
            pending_cards = [
                {**card, "status": "pending_revision"} if index in affected_indexes else card
                for index, card in enumerate(cards)
            ]
            return {
                "draft_itinerary": [dict(card) for card in pending_cards],
                "draft_cards": [cast(ItineraryCard, card) for card in pending_cards],
                "validation_results": {
                    "revision": {
                        "status": "failed",
                        "message": str(error),
                    }
                },
                "workflow_status": "error",
                "agent_response": f"局部重规划失败：{error}",
            }
    
    workflow.add_node("analyze", analyze_modification)
    workflow.add_node("regenerate", regenerate_cards)
    
    workflow.set_entry_point("analyze")
    workflow.add_conditional_edges(
        "analyze",
        lambda state: "regenerate" if state.get("affected_cards") else END,
        {"regenerate": "regenerate", END: END},
    )
    workflow.add_edge("regenerate", END)
    
    return workflow.compile()


# 创建全局图实例
# 这样在其他地方可以直接导入使用
travel_graph = create_travel_agent_graph()
modification_graph = create_modification_graph()
