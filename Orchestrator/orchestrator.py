"""Orchestrator：用 LangGraph 编排 SearchAgent → PlanAgent ⇄ ValidateAgent。

图结构：
  START → search → plan → validate
            validate --通过/达到最大轮数--> END
            validate --存在可修复严重问题--> plan（携带 feedback，至多修正一次）

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

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_env import component_python

SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = component_python("SearchAgent")
PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = component_python("PlanAgent")
VALIDATE_PY = ROOT / "ValidateAgent" / "validate.py"
VALIDATE_PYTHON = component_python("ValidateAgent")

MAX_ITERATIONS = 2

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
    audit: dict
    feedback: str
    iteration: int
    history: list


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
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env=env,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr).strip()}


def search_node(state: State) -> dict:
    basic = state.get("basic") or {}
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
    payload = {
        "profile": state.get("profile"),
        "preferences": state.get("preferences"),
        "recent_trips": state.get("recent_trips"),
        "recent_trip_summary": state.get("recent_trip_summary"),
        "skip_trip_summary": state.get("skip_trip_summary"),
        "search": state.get("search"),
        "basic": state.get("basic"),
    }
    if state.get("feedback"):
        payload["feedback"] = state["feedback"]
    if state.get("modify"):
        payload["modify"] = state["modify"]
    result = _call(PLAN_PYTHON, PLAN_PY, payload)
    search = dict(state.get("search") or {})
    if isinstance(result.get("food"), list):
        search["food"] = result["food"]
        search["food_by_anchor"] = result.get("food_by_anchor") or []
    return {"plan": result, "search": search, "iteration": state.get("iteration", 0) + 1}


def _plan_for_validate(plan: dict | None) -> dict | None:
    """审核实际选择和交通耗时，不重复发送地图折线或全部备选餐厅。"""
    if not isinstance(plan, dict):
        return plan
    trimmed = deepcopy(plan)
    trimmed.pop("food", None)
    trimmed.pop("food_by_anchor", None)
    trimmed["legs"] = [{k: v for k, v in leg.items() if k != "polyline"} for leg in plan.get("legs") or []]
    for block in trimmed.get("blocks") or []:
        block.pop("options", None)
    for style in trimmed.get("plans") or []:
        for day in style.get("itinerary") or []:
            for item in day.get("schedule") or []:
                item.pop("options", None)
    return trimmed


def _search_for_validate(search: dict | None, plan: dict | None) -> dict:
    """保留天气与已选资源的来源信息，减少无关候选带来的审核等待。"""
    search = search or {}
    names = {str(block.get("name") or "") for block in (plan or {}).get("blocks") or []}
    compact = {key: deepcopy(search[key]) for key in ("destination", "start_date", "end_date", "weather") if key in search}
    for key in ("poi", "hotels", "food", "events", "flights", "trains"):
        value = search.get(key)
        if isinstance(value, list):
            compact[key] = [item for item in value if isinstance(item, dict) and str(item.get("name") or item.get("title") or "") in names]
        elif isinstance(value, dict) and key in ("flights", "trains"):
            compact[key] = {direction: items[:5] for direction, items in value.items() if isinstance(items, list)}
    return compact


def _high_actionable_issues(audit: dict | None) -> list[dict]:
    """仅返回能够用现有行程/候选修复的严重问题；轻微建议不触发重跑。"""
    if not isinstance(audit, dict) or not isinstance(audit.get("issues"), list):
        return []
    return [issue for issue in audit["issues"] if isinstance(issue, dict)
            and str(issue.get("severity") or "").strip().lower() == "high"
            and issue.get("actionable") is not False
            and isinstance(issue.get("detail"), str) and issue["detail"].strip()
            and isinstance(issue.get("suggestion"), str) and issue["suggestion"].strip()]


def validate_node(state: State) -> dict:
    result = _call(
        VALIDATE_PYTHON,
        VALIDATE_PY,
        {
            "plan": _plan_for_validate(state.get("plan")),
            "profile": state.get("profile"),
            "preferences": state.get("preferences"),
            "recent_trips": state.get("recent_trips"),
            "search": _search_for_validate(state.get("search"), state.get("plan")),
            "basic": state.get("basic"),
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("passed"), bool):
        error = (result.get("error") if isinstance(result, dict) else None) or "审核结果不可用"
        result = {"passed": False, "issues": [], "feedback": "审核未返回有效结论", "error": error}
    elif result.get("error"):
        # 服务错误不是有效审核结论，不能报告已经通过。
        result = {**result, "passed": False}
    history = list(state.get("history") or [])
    history.append(
        {
            "iteration": state.get("iteration", 0),
            "passed": result.get("passed"),
            "issues": result.get("issues"),
            **({"error": result["error"]} if result.get("error") else {}),
        }
    )
    high_issues = _high_actionable_issues(result)
    feedback = None
    if result.get("passed") is False and not result.get("error") and high_issues:
        feedback = (
            "审核发现以下有证据、影响执行且可修复的严重问题。请针对这些问题重新规划，"
            "保持用户的目的地、日期、硬预算及偏好，使用已有真实候选；"
            "不要把轻微优化建议当成新增硬约束，也不要捏造未知报价。严重问题："
            + json.dumps(high_issues, ensure_ascii=False)
        )
    return {
        "audit": result,
        "history": history,
        "feedback": feedback,
    }


def should_continue(state: State) -> str:
    audit = state.get("audit") or {}
    if (audit.get("error") or audit.get("passed") is not False
            or (state.get("plan") or {}).get("error")
            or state.get("iteration", 0) >= MAX_ITERATIONS
            or not _high_actionable_issues(audit)):
        return "end"
    return "plan"


def should_validate(state: State) -> str:
    # modify 流程跳过 validate，直接返回修改后的计划
    if state.get("modify"):
        return "end"
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
    }


def build_graph():
    graph = StateGraph(State)
    graph.add_node("search", search_node)
    graph.add_node("prepare_memory", prepare_memory_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_edge(START, "search")
    graph.add_edge(START, "prepare_memory")
    graph.add_edge("search", "plan")
    graph.add_edge("prepare_memory", "plan")
    graph.add_conditional_edges("plan", should_validate, {"validate": "validate", "end": END})
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

        if "--stream" in sys.argv:
            graph = build_graph()
            thread_id = f"orchestrator-stream-{os.urandom(6).hex()}"
            config = {"configurable": {"thread_id": thread_id}}
            for chunk in graph.stream(data, config=config):
                node = next(iter(chunk), "") if isinstance(chunk, dict) else ""
                event = {"type": "node", "node": node}
                if node == "search" and isinstance(chunk, dict) and isinstance(chunk.get("search"), dict):
                    event["search"] = chunk["search"].get("search", chunk["search"])
                print(
                    json.dumps(event, ensure_ascii=False),
                    flush=True,
                )
            final = graph.get_state(config).values
            print(
                json.dumps(
                    {"type": "final", "data": _result_output(final, data)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return

        graph = build_graph()
        result = graph.invoke(data, {"configurable": {"thread_id": "orchestrator"}})
        output = _result_output(result, data)
    except Exception as exc:  # noqa: BLE001
        output = {"error": str(exc)}

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
