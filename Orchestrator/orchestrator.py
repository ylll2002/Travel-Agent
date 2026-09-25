"""Orchestrator：用 LangGraph 编排 SearchAgent → QuestionnaireAgent → PlanAgent ⇄ ValidateAgent。

图结构：
  START → search → questionnaire → plan → validate
            validate --通过/达到最大轮数--> END
            validate --不通过--> plan（携带 feedback 重新生成）

用法：
  echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03","profile":{...},"basic":{...},"answers":[...]}' \
    | python orchestrator.py

查看图：
  python orchestrator.py --graph
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

ROOT = Path(__file__).resolve().parent.parent

SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = ROOT / "SearchAgent" / ".venv" / "bin" / "python"
QUESTIONNAIRE_PY = ROOT / "QuestionnaireAgent" / "questionnaire.py"
QUESTIONNAIRE_PYTHON = ROOT / "QuestionnaireAgent" / ".venv" / "bin" / "python"
PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = ROOT / "PlanAgent" / ".venv" / "bin" / "python"
VALIDATE_PY = ROOT / "ValidateAgent" / "validate.py"
VALIDATE_PYTHON = ROOT / "ValidateAgent" / ".venv" / "bin" / "python"

MAX_ITERATIONS = 2


class State(TypedDict, total=False):
    destination: str
    start_date: str
    end_date: str
    profile: dict
    basic: dict
    answers: list
    search: dict
    questions: dict
    plan: dict
    audit: dict
    feedback: str
    iteration: int
    history: list


def _call(python: Path, script: Path, payload: dict) -> dict:
    proc = subprocess.run(
        [str(python), str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=600,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr).strip()}


def search_node(state: State) -> dict:
    result = _call(
        SEARCH_PYTHON,
        SEARCH_PY,
        {
            "destination": state["destination"],
            "start_date": state["start_date"],
            "end_date": state.get("end_date"),
        },
    )
    return {"search": result}


def questionnaire_node(state: State) -> dict:
    result = _call(
        QUESTIONNAIRE_PYTHON,
        QUESTIONNAIRE_PY,
        {
            "profile": state.get("profile"),
            "search": state.get("search"),
            "basic": state.get("basic"),
        },
    )
    return {"questions": result}


def plan_node(state: State) -> dict:
    payload = {
        "profile": state.get("profile"),
        "search": state.get("search"),
        "answers": state.get("answers"),
    }
    if state.get("feedback"):
        payload["feedback"] = state["feedback"]
    result = _call(PLAN_PYTHON, PLAN_PY, payload)
    return {"plan": result, "iteration": state.get("iteration", 0) + 1}


def validate_node(state: State) -> dict:
    result = _call(
        VALIDATE_PYTHON,
        VALIDATE_PY,
        {
            "plan": state.get("plan"),
            "profile": state.get("profile"),
            "search": state.get("search"),
            "basic": state.get("basic"),
            "answers": state.get("answers"),
        },
    )
    history = list(state.get("history") or [])
    history.append(
        {
            "iteration": state.get("iteration", 0),
            "passed": result.get("passed"),
            "issues": result.get("issues"),
        }
    )
    return {
        "audit": result,
        "history": history,
        "feedback": result.get("feedback"),
    }


def should_continue(state: State) -> str:
    if state.get("audit", {}).get("passed") or state.get("iteration", 0) >= MAX_ITERATIONS:
        return "end"
    return "plan"


def build_graph():
    graph = StateGraph(State)
    graph.add_node("search", search_node)
    graph.add_node("questionnaire", questionnaire_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_edge(START, "search")
    graph.add_edge("search", "questionnaire")
    graph.add_edge("questionnaire", "plan")
    graph.add_edge("plan", "validate")
    graph.add_conditional_edges("validate", should_continue, {"plan": "plan", "end": END})
    return graph.compile(checkpointer=MemorySaver())


def main() -> None:
    if "--graph" in sys.argv:
        graph = build_graph()
        print(graph.get_graph().draw_mermaid())
        return

    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        raw = " ".join(sys.argv[1:]).strip()
    if not raw:
        print(json.dumps({"error": "empty input"}, ensure_ascii=False))
        return

    try:
        data = json.loads(raw)
        graph = build_graph()
        result = graph.invoke(data, {"configurable": {"thread_id": "orchestrator"}})
        output = {
            "search": result.get("search"),
            "questions": result.get("questions"),
            "plan": result.get("plan"),
            "passed": (result.get("audit") or {}).get("passed"),
            "history": result.get("history"),
        }
    except Exception as exc:  # noqa: BLE001
        output = {"error": str(exc)}

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
