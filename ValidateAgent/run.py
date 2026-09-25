"""ValidateAgent 审核循环：PlanAgent 生成 → 审核 → 不过则退回修改，直到通过或达到最大轮数。"""

import json
import subprocess
import sys
from pathlib import Path

import validate

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = ROOT / "PlanAgent" / ".venv" / "bin" / "python"


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
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr).strip()}


def run_loop(context: dict, max_iterations: int = 2) -> dict:
    history: list[dict] = []
    feedback: str | None = None
    plan: dict | None = None
    for i in range(1, max_iterations + 1):
        plan = generate_plan(context, feedback)
        if not isinstance(plan, dict) or "error" in plan:
            return {"passed": False, "plan": plan, "history": history}

        audit = validate.validate_plan(
            plan,
            context.get("profile"),
            context.get("search"),
            context.get("basic"),
            context.get("answers"),
        )
        history.append(
            {
                "iteration": i,
                "passed": audit.get("passed"),
                "issues": audit.get("issues"),
            }
        )
        if audit.get("passed"):
            return {"passed": True, "plan": plan, "history": history}
        feedback = audit.get("feedback") or "请修正上述问题"

    return {
        "passed": False,
        "plan": plan,
        "history": history,
        "final_feedback": feedback,
    }


def main() -> None:
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        raw = " ".join(sys.argv[1:]).strip()
    if not raw:
        print(json.dumps({"error": "empty input"}, ensure_ascii=False))
        return
    try:
        context = json.loads(raw)
        result = run_loop(context)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
