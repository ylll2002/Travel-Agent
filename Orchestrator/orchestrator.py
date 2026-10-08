"""Orchestrator：用 LangGraph 编排 SearchAgent → PlanAgent ⇄ ValidateAgent。

图结构：
  START → search → plan → validate
            validate --通过/达到最大轮数--> END
            validate --存在可修复严重问题--> plan（携带 feedback，至多修正两次）

用法：
  echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03","profile":{...},"basic":{...}}' \
    | python orchestrator.py

查看图：
  python orchestrator.py --graph
"""

import json
from copy import deepcopy
import hashlib
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.audit import failed_audit, high_actionable_issues, history_entry, next_plan_revision, normalize_audit, repair_feedback, without_review
from shared.sources import merge_plan_sources

SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = ROOT / "SearchAgent" / ".venv" / "bin" / "python"
PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = ROOT / "PlanAgent" / ".venv" / "bin" / "python"
VALIDATE_PY = ROOT / "ValidateAgent" / "validate.py"
VALIDATE_PYTHON = ROOT / "ValidateAgent" / ".venv" / "bin" / "python"

MAX_ITERATIONS = 3

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000/api")


def _fetch_profile(user_id: str) -> dict:
    """从后端拉取用户画像；失败时返回空画像。"""
    url = f"{BACKEND_URL}/profile/{urllib.parse.quote(user_id)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            profile = json.loads(resp.read().decode("utf-8"))
        profile.pop("user_id", None)
        profile.pop("created_at", None)
        profile.pop("updated_at", None)
        return profile
    except Exception:
        return {}


def _fetch_preferences(user_id: str) -> dict:
    """先聚合行为信号，再返回用户长期偏好；失败时返回空偏好。"""
    url = f"{BACKEND_URL}/preferences/{urllib.parse.quote(user_id)}/aggregate"
    try:
        req = urllib.request.Request(
            url,
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pref = json.loads(resp.read().decode("utf-8"))
        return pref.get("preferences") or {}
    except Exception:
        return {}


def _fetch_recent_trips(user_id: str, limit: int = 3) -> list[dict]:
    """拉取最近 N 条历史行程，只保留对规划有用的信号，避免 final_plan 撑爆 prompt。"""
    url = f"{BACKEND_URL}/trip-memory/{urllib.parse.quote(user_id)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            memories = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    recent: list[dict] = []
    for m in memories[:limit]:
        edits = m.get("user_edits") or []
        if isinstance(edits, list):
            edits = edits[:3]
        recent.append(
            {
                "destination": m.get("destination") or "",
                "start_date": m.get("start_date") or "",
                "end_date": m.get("end_date") or "",
                "chosen_plan_style": m.get("chosen_plan_style") or "",
                "rating": m.get("rating"),
                "feedback": m.get("feedback") or "",
                "user_edits": edits,
            }
        )
    return recent


def _trip_hash(recent_trips: list[dict]) -> str:
    payload = json.dumps(recent_trips, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fetch_trip_summary_cache(user_id: str) -> dict | None:
    url = f"{BACKEND_URL}/memory-cache/{urllib.parse.quote(user_id)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class State(TypedDict, total=False):
    user_id: str
    destination: str
    start_date: str
    end_date: str
    profile: dict
    preferences: dict
    recent_trips: list
    recent_trip_summary: str
    skip_trip_summary: bool
    basic: dict
    modify: dict
    search: dict
    plan: dict
    audit: dict | None
    feedback: str | None
    iteration: int
    history: list
    repair_count: int
    repair_error: str | None


def _call(python: Path, script: Path, payload: dict) -> dict:
    import platform

    python_str = str(python)
    # 仅在 Windows 上做 bin→Scripts 路径替换
    if platform.system() == "Windows":
        python_str = python_str.replace("bin\\python", "Scripts\\python.exe").replace("bin/python", "Scripts\\python.exe")
    # 兜底：替换后路径不存在时回退到原路径（不混用 sys.executable，避免 venv 错乱）
    if not os.path.exists(python_str):
        python_str = str(python)

    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)

    proc = subprocess.run(
        [python_str, str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr).strip()}


def search_node(state: State) -> dict:
    basic = state.get("basic")
    if not isinstance(basic, dict):
        basic = (state.get("plan") or {}).get("basic") or {}
    payload = {
        "destination": state["destination"],
        "start_date": state["start_date"],
        "end_date": state.get("end_date"),
    }
    if basic.get("origin"):
        payload["origin"] = basic["origin"]
    if basic.get("food_keyword"):
        payload["food_keyword"] = basic["food_keyword"]
    if state.get("profile"):
        payload["profile"] = state.get("profile")
    # 保留完整需求，搜索与规划使用同一套人数、日期、预算和偏好。
    if basic:
        payload["basic"] = {**basic, "start_date": state["start_date"], "end_date": state.get("end_date")}
    result = _call(SEARCH_PYTHON, SEARCH_PY, payload)
    return {"search": result}


def plan_node(state: State) -> dict:
    basic = state.get("basic")
    if not isinstance(basic, dict):
        basic = (state.get("plan") or {}).get("basic") or {}
    # 初始的全城美食只用于前端候选展示，规划时不传入（规划用景点锚点周边的美食）。
    search_for_plan = dict(state.get("search") or {})
    if not state.get("feedback"):
        search_for_plan.pop("food", None)
    search_for_plan.pop("food_preview", None)
    payload = {
        "profile": state.get("profile"),
        "preferences": state.get("preferences"),
        "recent_trips": state.get("recent_trips"),
        "recent_trip_summary": state.get("recent_trip_summary"),
        "skip_trip_summary": state.get("skip_trip_summary"),
        "search": search_for_plan,
        "basic": basic,
    }
    if state.get("feedback"):
        payload["feedback"] = state["feedback"]
    if state.get("feedback") and state.get("plan"):
        payload["audit_repair"] = True
        payload["repair_issues"] = _high_actionable_issues(state.get("audit"))
        payload["plan"] = without_review(state["plan"])
    elif state.get("modify"):
        payload["modify"] = state["modify"]
        if state.get("plan"):
            payload["plan"] = without_review(state["plan"])
    result = without_review(_call(PLAN_PYTHON, PLAN_PY, payload))
    repairing = bool(state.get("feedback") and state.get("plan"))
    if repairing and (result.get("error") or not isinstance(result.get("blocks"), list)):
        # Keep the last usable itinerary if a repair call fails.
        return {"plan": without_review(state["plan"]), "repair_error": str(result.get("error") or "修复没有返回完整行程"),
                "iteration": state.get("iteration", 0) + 1,
                "repair_count": state.get("repair_count", 0) + 1}
    if state.get("modify") and not repairing and isinstance(result.get("basic"), dict):
        basic = deepcopy(result["basic"])
    result.update(revision=next_plan_revision(state.get("plan")), basic=deepcopy(basic))
    search = merge_plan_sources(state.get("search"), result)
    # Keep city-wide UI candidates while the audit uses the actual anchor sources.
    original_search = state.get("search") or {}
    if "food_preview" not in search and isinstance(original_search.get("food"), list):
        search["food_preview"] = deepcopy(original_search["food"])
    return {"plan": result, "search": search, "iteration": state.get("iteration", 0) + 1,
            "audit": None, "feedback": None, "basic": basic, "repair_error": None,
            "repair_count": state.get("repair_count", 0) + int(repairing)}


def _high_actionable_issues(audit: dict | None) -> list[dict]:
    return high_actionable_issues(audit)


def validate_node(state: State) -> dict:
    if state.get("repair_error"):
        result = {**(state.get("audit") or failed_audit("自动修复失败", "请重试", state.get("plan"))),
                  "passed": False, "error": "自动修复失败：" + state["repair_error"],
                  "feedback": "现有行程已保留，请稍后重试自动修复。"}
    elif (state.get("plan") or {}).get("error"):
        result = failed_audit("规划失败", "未生成有效方案，无法审核", state.get("plan"))
    else:
        result = _call(
            VALIDATE_PYTHON,
            VALIDATE_PY,
            {
                "plan": state.get("plan"),
                "profile": state.get("profile"),
                "preferences": state.get("preferences"),
                "recent_trips": state.get("recent_trips"),
                "search": state.get("search"),
                "basic": state.get("basic"),
            },
        )
    try:
        if isinstance(result, dict) and "plan_revision" in result:
            expected = (state.get("plan") or {}).get("revision")
            if result["plan_revision"] != expected or type(result["plan_revision"]) is not type(expected):
                raise ValueError("review version mismatch")
        result = normalize_audit(result, state.get("plan"), source="mixed")
    except (ValueError, TypeError):
        error = (result.get("error") if isinstance(result, dict) else None) or "审核结果不可用"
        result = failed_audit(error, "审核未返回有效结论", state.get("plan"))
    history = list(state.get("history") or [])
    history.append(history_entry(state.get("iteration", 0), result))
    return {"audit": result, "history": history, "feedback": repair_feedback(result)}


def should_continue(state: State) -> str:
    audit = state.get("audit") or {}
    if (audit.get("error") or audit.get("passed") is not False
            or (state.get("plan") or {}).get("error")
            or state.get("iteration", 0) >= MAX_ITERATIONS
            or not _high_actionable_issues(audit)):
        return "end"
    return "plan"


def should_validate(state: State) -> str:
    # Every new snapshot, including automatic repairs, must be reviewed.
    return "validate"


def _memory_updates(state: dict) -> dict:
    """补齐记忆上下文：画像、偏好、最近行程及语义摘要缓存。"""
    updates: dict = {}
    user_id = state.get("user_id")
    if not user_id:
        return updates
    if not state.get("profile"):
        updates["profile"] = _fetch_profile(user_id)
    if not state.get("preferences"):
        updates["preferences"] = _fetch_preferences(user_id)
    if not state.get("recent_trips"):
        updates["recent_trips"] = _fetch_recent_trips(user_id)
    recent_trips = state.get("recent_trips") or updates.get("recent_trips") or []
    # 摘要只在用户确认计划并评价后生成（见 trip-memory 路由），规划阶段仅读取缓存。
    updates["skip_trip_summary"] = True
    if recent_trips:
        cache = _fetch_trip_summary_cache(user_id)
        if (
            cache
            and cache.get("trip_summary_hash") == _trip_hash(recent_trips)
            and cache.get("trip_summary")
        ):
            updates["recent_trip_summary"] = cache["trip_summary"]
    return updates


def prepare_memory_node(state: State) -> dict:
    return _memory_updates(state)


def _result_output(result: dict, data: dict) -> dict:
    saved_memory = None
    return {
        "search": result.get("search"),
        "plan": result.get("plan"),
        "passed": (result.get("audit") or {}).get("passed"),
        "audit": result.get("audit"),
        "history": result.get("history"),
        "saved_memory": saved_memory,
        "review_context": {key: result.get(key) for key in ("profile", "preferences", "recent_trips")},
        "workflow": {"repair_count": result.get("repair_count", 0), "repair_limit": MAX_ITERATIONS - 1,
                     "stop_reason": "error" if (result.get("audit") or {}).get("error") else
                       "passed" if (result.get("audit") or {}).get("passed") else
                       "limit" if result.get("iteration", 0) >= MAX_ITERATIONS else "needs_information"},
    }


def build_graph(repair_existing=False):
    graph = StateGraph(State)
    graph.add_node("search", search_node)
    graph.add_node("prepare_memory", prepare_memory_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    if repair_existing:
        graph.add_edge(START, "validate")
    else:
        graph.add_edge(START, "search")
        graph.add_edge(START, "prepare_memory")
    graph.add_edge("search", "plan")
    graph.add_edge("prepare_memory", "plan")
    graph.add_conditional_edges("plan", should_validate, {"validate": "validate", "end": END})
    graph.add_conditional_edges("validate", should_continue, {"plan": "plan", "end": END})
    return graph.compile(checkpointer=MemorySaver())



def stream_events(graph, data, config):
    """Expose each usable snapshot and verdict before starting the next repair."""
    state = deepcopy(data)
    yield {"type": "node", "node": "start", "stage": "reviewing" if data.get("plan") else "planning",
           "repair_count": 0, "repair_limit": MAX_ITERATIONS - 1}
    for chunk in graph.stream(data, config=config):
        for node, update in chunk.items():
            state.update(update or {})
            event = {"type": "node", "node": node}
            if node == "search":
                event["search"] = state.get("search")
            if node in ("plan", "validate"):
                plan = state.get("plan") or {}
                event.update(plan=plan, search=state.get("search"), history=state.get("history") or [],
                             repair_count=state.get("repair_count", 0), repair_limit=MAX_ITERATIONS - 1,
                             review_context={k: state.get(k) for k in ("profile", "preferences", "recent_trips")})
                if node == "plan":
                    event.update(stage="reviewing", audit=None)
                else:
                    event.update(stage="repairing" if should_continue(state) == "plan" else
                                 (state.get("audit") or {}).get("status", "error"), audit=state.get("audit"))
            yield event
    yield {"type": "final", "data": _result_output(state, data)}

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

        if "--stream" in sys.argv:
            repairing = "--repair" in sys.argv
            if repairing:
                data.update(iteration=1, repair_count=0, history=[], audit=None, feedback=None)
            graph = build_graph(repair_existing=repairing)
            config = {"configurable": {"thread_id": f"orchestrator-stream-{os.urandom(6).hex()}"}}
            for event in stream_events(graph, data, config):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            return

        graph = build_graph()
        result = graph.invoke(data, {"configurable": {"thread_id": "orchestrator"}})
        output = _result_output(result, data)
    except Exception as exc:  # noqa: BLE001
        if "--stream" in sys.argv:
            print(json.dumps({"type": "error", "error": "自动处理未完成，请重试"}, ensure_ascii=False), flush=True)
            return
        output = {"error": str(exc)}

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
