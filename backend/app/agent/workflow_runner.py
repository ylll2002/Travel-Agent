"""LangGraph 工作流执行与节点级 trace。"""

from collections.abc import Callable
from copy import deepcopy
from time import perf_counter
from typing import Any, cast

from app.agent.state import InitialTravelAgentState, TravelAgentState, TravelAgentUpdate

StateInput = InitialTravelAgentState | TravelAgentState


class WorkflowRunner:
    """统一执行工作流，并处理 LangGraph update 到状态的合并。"""

    def run(self, graph, initial_state: StateInput) -> TravelAgentState:
        """执行工作流并返回带诊断事件的最终状态。"""
        state = cast_state(deepcopy(cast(dict[str, Any], initial_state)))
        events = list(state.get("workflow_events", []))
        started_at = perf_counter()
        events.append({"event": "workflow_started", "status": "running", "at_step": 0})

        try:
            for step, node_update in enumerate(graph.stream(initial_state, stream_mode="updates"), start=1):
                if step > 30:
                    raise RuntimeError("工作流超过 30 个节点步骤，已停止以避免路由循环")
                node_name, update = next(iter(node_update.items()))
                merge_update(state, update)
                events.append({
                    "event": "node_completed",
                    "node": node_name,
                    "status": "completed",
                    "step": step,
                    "elapsed_ms": round((perf_counter() - started_at) * 1000),
                    "writes": list(update.keys()),
                })
                state["workflow_events"] = events

            events.append({
                "event": "workflow_completed",
                "status": "completed",
                "steps": len(events) - 1,
                "elapsed_ms": round((perf_counter() - started_at) * 1000),
            })
            state["workflow_events"] = events
            return state
        except Exception as error:
            events.append({
                "event": "workflow_failed",
                "status": "failed",
                "step": len([item for item in events if item.get("event") == "node_completed"]) + 1,
                "elapsed_ms": round((perf_counter() - started_at) * 1000),
                "error": str(error),
            })
            state["workflow_events"] = events
            state["workflow_status"] = "error"
            state["agent_response"] = f"工作流在开发诊断中止：{error}"
            return state

    def stream_trace(
        self,
        graph,
        initial_state: StateInput,
        changed_fields: Callable[[dict], dict] | None = None,
    ) -> tuple[TravelAgentState, list[dict]]:
        """执行工作流并返回每个节点的输入、输出和后续状态快照。"""
        current_state = cast_state(deepcopy(cast(dict[str, Any], initial_state)))
        trace: list[dict] = []
        field_summary = changed_fields or (lambda update: update)

        for node_update in graph.stream(initial_state, stream_mode="updates"):
            node_name, update = next(iter(node_update.items()))
            input_state = deepcopy(current_state)
            merge_update(current_state, update)
            trace.append({
                "node": node_name,
                "data_flow": {
                    "reads": list(input_state.keys()),
                    "writes": list(update.keys()),
                    "key_payload": field_summary(update),
                },
                "input_state": input_state,
                "output_update": update,
                "state_passed_forward": deepcopy(current_state),
            })

        for index, item in enumerate(trace):
            item["next_node"] = trace[index + 1]["node"] if index + 1 < len(trace) else "__end__"
        return current_state, trace


def merge_update(state: TravelAgentState, update: TravelAgentUpdate) -> None:
    """按 LangGraph state reducer 规则合并单个节点更新。"""
    mutable_state = cast(dict[str, Any], state)
    for field, value in cast(dict[str, Any], update).items():
        if field == "messages":
            mutable_state[field] = [*mutable_state.get(field, []), *value]
        else:
            mutable_state[field] = value


def cast_state(value: dict) -> TravelAgentState:
    return value  # type: ignore[return-value]
