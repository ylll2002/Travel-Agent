import json
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel
import sys

ROOT = Path(__file__).resolve().parents[4]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_env import component_python, subprocess_env
QUESTION_PY = ROOT / "QuestionAgent" / "agent.py"
QUESTION_PYTHON = component_python("QuestionAgent", fallback=component_python("PlanAgent"))
SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = component_python("SearchAgent")

router = APIRouter(prefix="/question", tags=["question"])


def _subprocess_env() -> dict:
    """子进程环境：UTF-8 标准流 + 清理 macOS venv 遗留变量。

    必须与父进程的 encoding="utf-8" 配套，否则子进程会按 GBK 输出中文，
    父进程按 UTF-8 解码即抛 UnicodeDecodeError。
    """
    return subprocess_env()


class QuestionRequest(BaseModel):
    messages: list[dict]
    has_plan: bool = False
    trip_data: dict | None = None


@router.post("")
def ask_question(payload: QuestionRequest) -> dict:
    try:
        proc = subprocess.run(
            [str(QUESTION_PYTHON), str(QUESTION_PY)],
            input=json.dumps(
                {
                    "messages": payload.messages,
                    "has_plan": payload.has_plan,
                    "trip_data": payload.trip_data or {},
                },
                ensure_ascii=False,
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
            timeout=75,
        )
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            return {"error": "需求识别返回异常，请重新描述旅行需求"}
        if result.get("action") == "explain" and result.get("query"):
            try:
                web = subprocess.run(
                    [str(SEARCH_PYTHON), str(SEARCH_PY), "--web", str(result["query"])],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=_subprocess_env(),
                    timeout=60,
                )
                web_data = json.loads(web.stdout)
                result["links"] = [
                    {"title": item.get("title") or "", "url": item.get("url") or ""}
                    for item in (web_data.get("results") or [])
                    if item.get("url")
                ]
            except Exception:
                result["links"] = []
        return result
    except json.JSONDecodeError:
        return {"error": "需求识别返回异常，请重新描述旅行需求"}
    except subprocess.TimeoutExpired:
        return {"error": "需求识别超时，请重试；已经填写的信息会保留"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
