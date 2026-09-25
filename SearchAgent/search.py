"""SearchAgent 的统一入口：JSON 或自然语言输入 → 结构化 JSON 输出。

用法（JSON 输入）：
  echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-05"}' | python search.py

用法（自然语言输入，会先用 LLM 解析成 JSON）：
  python search.py "宁波 10月1日到10月5日"
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import tools

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PARSE_SYSTEM_PROMPT = (
    "从用户的旅行查询中抽取信息，输出 JSON，字段："
    "destination（目的地，字符串，必填）、"
    "start_date（开始日期，格式 YYYY-MM-DD，必填）、"
    "end_date（结束日期，格式 YYYY-MM-DD，可选）、"
    "origin（出发地，可选）。如果日期没有年份，默认今年。"
    "只输出 JSON 本身，不要任何多余文字或代码块。"
)


def parse_nl(query: str) -> dict:
    """用 LLM 把自然语言解析成结构化 JSON。"""
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
        messages=[
            {"role": "system", "content": PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_object"},
        timeout=60,
    )
    content = (resp.choices[0].message.content or "{}").strip()
    # 去掉可能包裹的 ```json ... ``` 代码块
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def main() -> None:
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        raw = " ".join(sys.argv[1:]).strip()

    if not raw:
        print(json.dumps({"error": "empty input"}, ensure_ascii=False))
        return

    # 先按 JSON 解析，失败则按自然语言解析
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        try:
            data = parse_nl(raw)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": f"自然语言解析失败：{exc}"}, ensure_ascii=False))
            return

    try:
        result = tools.run_search(data)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
