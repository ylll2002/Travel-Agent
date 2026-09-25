from fastapi import APIRouter, Query

from app.agent.state_factory import create_initial_state
from app.agent.travel_agent import TravelAgent
from app.agent.workflow_runner import WorkflowRunner
from app.schemas import ChatRequest, ChatResponse, WorkflowTraceResponse

router = APIRouter(prefix="/agent", tags=["agent"])

agent = TravelAgent()
workflow_runner = WorkflowRunner()


def _changed_fields(update: dict) -> dict:
    """提取节点真正写入共享状态的字段。"""
    return {field: value for field, value in update.items() if field != "messages"}


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    reply, session_id, state = agent.chat(request)
    payload = {
        "reply": reply,
        "session_id": session_id or None,
        "workflow_status": state.get("workflow_status") if state else None,
        "current_stage": state.get("current_stage") if state else None,
        "pending_question": state.get("pending_question") if state else None,
        "trip_brief": state.get("trip_brief", {}) if state else {},
        "candidate_destinations": state.get("candidate_destinations", []) if state else [],
        "time_candidates": state.get("time_candidates", []) if state else [],
        "draft_cards": state.get("draft_cards", []) if state else [],
        "card_dependencies": state.get("card_dependencies", {}) if state else {},
        "evidence_refs": state.get("evidence_refs", []) if state else [],
        "validation_results": state.get("validation_results", {}) if state else {},
        "affected_cards": state.get("affected_cards", []) if state else [],
        "modification_scope": state.get("modification_scope") if state else None,
        "revision_comparison": state.get("revision_comparison", []) if state else [],
        "specialist_results": state.get("specialist_results", []) if state else [],
        "agent_tasks": state.get("agent_tasks", []) if state else [],
        "workflow_events": state.get("workflow_events", []) if state else [],
    }
    return ChatResponse.model_validate(payload)


@router.get("/workflow")
def workflow() -> dict[str, str]:
    """返回主 Agent 工作流的 Mermaid 表示。"""
    return {"mermaid": agent.graph.get_graph().draw_mermaid()}


def _build_trace(messages: list[dict]) -> WorkflowTraceResponse:
    """执行主工作流并返回节点之间传递的共享状态。"""
    user_messages = [message["content"] for message in messages if message["role"] == "user"]
    topic = user_messages[-1] if user_messages else ""
    state = create_initial_state(messages=messages, topic=topic, session_id="trace")
    _, trace = workflow_runner.stream_trace(agent.graph, state, _changed_fields)

    return WorkflowTraceResponse.model_validate({
        "initial_state": dict(state),
        "trace": trace,
        "executed_nodes": [item["node"] for item in trace],
    })


@router.post("/workflow/trace", response_model=WorkflowTraceResponse)
def workflow_trace(request: ChatRequest) -> WorkflowTraceResponse:
    return _build_trace([
        {"role": message.role, "content": message.content}
        for message in request.messages
    ])


@router.get("/workflow/trace", response_model=WorkflowTraceResponse)
def workflow_trace_get(
    topic: str = Query(..., min_length=1, description="用户的旅行想法"),
) -> WorkflowTraceResponse:
    """通过浏览器 URL 执行工作流并返回完整数据流。"""
    return _build_trace([{"role": "user", "content": topic}])

