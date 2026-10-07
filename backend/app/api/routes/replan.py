"""Explicit slot replacement, separate from conversational intent routing."""

import json
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_env import component_python, component_script, subprocess_env

router = APIRouter(prefix="/plan", tags=["plan"])


class ReplanRequest(BaseModel):
    plan: dict
    revision: int = Field(ge=0)
    plan_style: str = Field(min_length=1)
    target_block_ids: list[str] = Field(min_length=1, max_length=10)
    instruction: str = Field(min_length=1, max_length=2000)
    locked_block_ids: list[str] = Field(default_factory=list)
    profile: dict | None = None
    basic: dict | None = None


def run_module(module: str, script: str, payload: dict, timeout: int) -> dict:
    """以 JSON 为输入调用组件脚本；解释器路径交由 agent_env 跨平台解析。"""
    proc = subprocess.run(
        [str(component_python(module)), str(component_script(module, script))],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=subprocess_env(),
    )
    if proc.returncode != 0:
        raise HTTPException(502, f"{module}运行失败，请查看服务日志并检查依赖配置。")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"{module}未返回有效结果，原方案已保留。") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, f"{module}返回结果格式错误。")
    return result


@router.post("/replan")
def replan(payload: ReplanRequest) -> dict:
    plan = payload.plan
    if payload.revision != plan.get("revision", 0):
        raise HTTPException(409, "方案版本已变化，请重新选择活动。")
    blocks = plan.get("blocks") or []
    if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
        raise HTTPException(422, "方案活动格式错误。")
    targets = set(payload.target_block_ids)
    selected = [b for b in blocks if b.get("id") in targets]
    if len(targets) != len(payload.target_block_ids) or len(selected) != len(targets) or any(b.get("plan_style") != payload.plan_style for b in selected):
        raise HTTPException(422, "选中活动ID无效或不属于当前方案。")
    if not payload.instruction.strip() or not plan.get("destination") or not plan.get("start_date"):
        raise HTTPException(422, "缺少原因、目的地或日期。")
    if any(b.get("type") not in ("景点", "美食", "酒店", "活动", "交通") for b in selected):
        raise HTTPException(422, "该类别不能重新规划。")
    try:
        basic = payload.basic or {}
        search_payload = {"destination": plan["destination"], "start_date": plan["start_date"], "end_date": plan.get("end_date"), "origin": basic.get("origin"), "basic": basic, "profile": payload.profile}
        search = run_module("SearchAgent", "search.py", search_payload, 180)
        if search.get("error"):
            raise HTTPException(502, "搜索失败，原方案已保留。")
        result = run_module("PlanAgent", "replan.py", {**payload.model_dump(), "search": search}, 480)
        if "没有可用的同类替代项" in str(result.get("error", "")):
            search = run_module("SearchAgent", "search.py", {**search_payload, "force_refresh": True}, 180)
            if search.get("error"):
                raise HTTPException(502, "补充搜索失败，原方案已保留。")
            result = run_module("PlanAgent", "replan.py", {**payload.model_dump(), "search": search}, 480)
        if result.get("error"):
            raise HTTPException(422, result["error"])
        return result
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "重新规划超时，原方案已保留，请重试。") from exc
    except OSError as exc:
        raise HTTPException(503, "无法启动Agent，请检查各模块的虚拟环境。") from exc
