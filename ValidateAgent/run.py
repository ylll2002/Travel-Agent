"""ValidateAgent 审核循环：PlanAgent 生成 → 审核 → 不过则退回修改，直到通过或达到最大轮数。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import validate

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
sys.path.insert(0, str(ROOT))
from shared.audit import failed_audit, history_entry, normalize_audit, repair_feedback

PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = ROOT / "PlanAgent" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def generate_plan(context: dict, feedback: str | None = None) -> dict:
    payload = dict(context)
    if feedback:
        payload["feedback"] = feedback
    proc = subprocess.run(
        [str(PLAN_PYTHON), str(PLAN_PY)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode:
        return {"error": "PlanAgent执行失败，请检查服务日志"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "PlanAgent未返回有效JSON"}


def run_loop(context: dict, max_iterations: int = 2) -> dict:
    if not isinstance(context, dict):
        raise ValueError("规划上下文必须是JSON对象")
    if type(max_iterations) is not int or not 1 <= max_iterations <= 2:
        raise ValueError("审核循环仅允许1至2轮")
    history: list[dict] = []
    feedback = None
    plan = None
    for i in range(1, max_iterations + 1):
        try:
            plan = generate_plan(context, feedback)
        except (OSError, subprocess.TimeoutExpired):
            plan = {"error": "规划服务不可用或超时"}
        if not isinstance(plan, dict) or "error" in plan:
            audit = failed_audit("规划失败", "未生成有效方案，无法审核")
        else:
            try:
                audit = normalize_audit(validate.validate_plan(
                    plan=plan,
                    profile=context.get("profile"),
                    preferences=context.get("preferences"),
                    recent_trips=context.get("recent_trips"),
                    search=context.get("search"),
                    basic=context.get("basic"),
                ), plan)
            except (ValueError, TypeError):
                audit = failed_audit("审核结果不可用", "审核未返回有效结论", plan)
        history.append(history_entry(i, audit))
        feedback = repair_feedback(audit)
        if audit["passed"] or feedback is None or i == max_iterations:
            return {"passed": audit["passed"], "plan": plan, "audit": audit,
                    "history": history, "final_feedback": audit["feedback"]}


def main() -> None:
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        raw = " ".join(sys.argv[1:]).strip()
    if not raw:
        print(json.dumps({"passed": False, "plan": None, "audit": failed_audit("empty input", "请提供规划上下文"), "history": []}, ensure_ascii=False))
        return
    try:
        context = json.loads(raw)
        result = run_loop(context)
    except Exception:  # noqa: BLE001
        result = {"passed": False, "plan": None, "audit": failed_audit("审核循环失败", "请检查输入及服务配置"), "history": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
