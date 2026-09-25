"""TravelAgent 主类：使用 LangGraph 实现多 Agent 协作。

这个类是外部接口，接收用户请求，调用 LangGraph 工作流，返回结果。
"""

from typing import cast

from app.agent.graph import modification_graph, travel_graph
from app.agent.interaction import apply_revision_decision, apply_user_selection
from app.agent.session_store import PersistentSessionStore
from app.agent.state import TravelAgentState
from app.agent.state_factory import create_initial_state
from app.agent.workflow_runner import WorkflowRunner
from app.db import SessionLocal
from app.schemas import ChatRequest


class TravelAgent:
    """Travel assistant with LangGraph multi-agent system.
    
    使用 LangGraph 框架实现的旅行助手，支持：
    1. 从模糊想法到目的地推荐
    2. 多 Agent 协作生成行程
    3. 单卡片修改和影响分析
    """

    def __init__(self, session_store=None):
        """初始化 Agent，加载工作流图。"""
        self.graph = travel_graph
        self.modification_graph = modification_graph
        self.session_store = session_store or PersistentSessionStore(SessionLocal)
        self.sessions = getattr(self.session_store, "_sessions", {})
        self.workflow_runner = WorkflowRunner()
    
    def chat(self, request: ChatRequest) -> tuple[str, str, TravelAgentState]:
        """处理用户消息，返回 Agent 回复。
        
        工作流程：
        1. 构建初始状态（State）
        2. 判断是新规划还是修改请求
        3. 调用对应的 LangGraph 工作流
        4. 提取最终结果返回给用户
        """
        
        # 提取用户最新消息
        user_messages = [m.content for m in request.messages if m.role == "user"]
        last_message = user_messages[-1] if user_messages else ""
        
        # 判断是否为修改请求
        # 简单规则：包含"修改"、"换"、"改"等关键词
        if any(keyword in last_message for keyword in ["接受修改", "采用新版本", "保留旧版本", "撤销修改"]):
            session_id = request.session_id or self._new_session_id()
            result = self._handle_revision_decision(last_message, session_id)
            return self._format_response(result), session_id, result

        is_modification = any(keyword in last_message for keyword in ["修改", "换", "改", "替换"])
        
        if is_modification:
            session_id = request.session_id or self._new_session_id()
            result = self._handle_modification(request, last_message, session_id)
            return self._format_response(result), session_id, result
        else:
            # 新规划流程
            session_id = request.session_id or self._new_session_id()
            result = self._run_planning(request, last_message, session_id)
            return self._format_response(result), session_id, result

    def _new_session_id(self) -> str:
        import uuid
        return str(uuid.uuid4())

    def _run_planning(self, request: ChatRequest, topic: str, session_id: str) -> TravelAgentState:
        previous = self.session_store.get(session_id)
        if previous:
            initial_state = cast(TravelAgentState, dict(previous))
            initial_state["messages"] = [
                {"role": m.role, "content": m.content} for m in request.messages
            ]
            initial_state["user_action"] = "continue_planning"
            initial_state["pending_question"] = ""
            initial_state["workflow_status"] = "running"
            self._apply_user_selection(initial_state, topic)
        else:
            initial_state = self._initial_state(request, topic, session_id)

        result = self._run_graph_with_trace(self.graph, initial_state)
        self.session_store.save(session_id, result)
        return result

    def _run_graph_with_trace(self, graph, initial_state: TravelAgentState) -> TravelAgentState:
        """兼容入口：委托统一工作流执行器。"""
        return self.workflow_runner.run(graph, initial_state)

    def _apply_user_selection(self, state: TravelAgentState, message: str) -> None:
        """兼容入口：委托交互模块处理用户选择。"""
        apply_user_selection(state, message)

    def _initial_state(self, request: ChatRequest, topic: str, session_id: str) -> TravelAgentState:
        return create_initial_state(
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            topic=topic,
            session_id=session_id,
        )

    def _format_response(self, result: TravelAgentState) -> str:
        response = result.get("agent_response", "正在为您规划行程...")
        pending_question = result.get("pending_question", "")
        if pending_question:
            response += f"\n\n{pending_question}"
        return response

    def _handle_revision_decision(self, decision: str, session_id: str) -> TravelAgentState:
        """确认或撤销当前会话中待确认的局部重规划结果。"""
        previous = self.session_store.get(session_id)
        if previous is None:
            return cast(TravelAgentState, {
                "session_id": session_id,
                "workflow_status": "waiting_for_user",
                "pending_question": "请先生成并修改一份行程草案。",
                "agent_response": "当前会话没有待确认的修改结果。",
            })

        state = cast(TravelAgentState, dict(previous))
        state = apply_revision_decision(state, decision)
        self.session_store.save(session_id, state)
        return state
    
    def _handle_modification(
        self,
        request: ChatRequest,
        modification: str,
        session_id: str,
    ) -> TravelAgentState:
        """处理修改行程的请求。
        
        调用修改工作流：analyze → regenerate
        """
        
        previous = self.session_store.get(session_id)
        if previous is None:
            return {
                **self._initial_state(request, modification, session_id),
                "workflow_status": "waiting_for_user",
                "pending_question": "请先生成一份行程草案，再进行卡片修改。",
                "agent_response": "当前会话还没有可修改的行程。",
            }

        state = cast(TravelAgentState, dict(previous))
        state["messages"] = [{"role": m.role, "content": m.content} for m in request.messages]
        state["modification_request"] = modification
        state["user_action"] = "modify_card"
        state["workflow_status"] = "running"
        
        try:
            result = self._run_graph_with_trace(self.modification_graph, state)
            self.session_store.save(session_id, result)
            return result
        except Exception as e:
            state["workflow_status"] = "error"
            state["agent_response"] = f"处理修改时出错：{str(e)}"
            return state

