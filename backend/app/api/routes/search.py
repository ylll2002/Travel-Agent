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
SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = component_python("SearchAgent")

router = APIRouter(prefix="/search", tags=["search"])


def _subprocess_env() -> dict:
    """子进程环境：UTF-8 标准流 + 清理 macOS venv 遗留变量。

    必须与父进程的 encoding="utf-8" 配套，否则子进程会按 GBK 输出中文，
    父进程按 UTF-8 解码即抛 UnicodeDecodeError。
    """
    return subprocess_env()


class SearchRequest(BaseModel):
    query: str | None = None
    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    origin: str | None = None


@router.post("")
def search(payload: SearchRequest) -> dict:
    destination = payload.destination
    start_date = payload.start_date
    end_date = payload.end_date
    origin = payload.origin

    if payload.query and not destination:
        try:
            proc = subprocess.run(
                [str(SEARCH_PYTHON), str(SEARCH_PY), "--parse"],
                input=payload.query,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env(),
                timeout=120,
            )
            parsed = json.loads(proc.stdout)
            destination = parsed.get("destination")
            start_date = parsed.get("start_date")
            end_date = parsed.get("end_date")
            origin = parsed.get("origin")
        except Exception:
            parsed = {}

    if not destination or not start_date:
        return {"error": "无法从输入中解析出目的地或日期"}

    search_payload = {
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
    }
    if origin:
        search_payload["origin"] = origin

    try:
        proc = subprocess.run(
            [str(SEARCH_PYTHON), str(SEARCH_PY)],
            input=json.dumps(search_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
            timeout=180,
        )
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout or proc.stderr).strip()}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
