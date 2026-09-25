"""QuestionnaireAgent：根据画像 + 基础信息 + 搜索结果，生成 4~6 道选择题问卷。

作用：在用户选定目的地/日期/预算等基础信息、并拿到 SearchAgent 的搜索结果后，
拟一份简短问卷，把 PlanAgent 需要的偏好问得更细（例如选景点、选酒店、选美食）。

输入（JSON）：
  {"profile": {...画像...}, "search": {...搜索结果...}, "basic": {...基础信息...}}
  profile / basic 可省略。

输出（JSON）：
  {"questions":[{"id":"q1","type":"single|multiple","question":"...","options":["..."],"max_select":null|数字}]}
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

QUESTIONNAIRE_SYSTEM_PROMPT = (
    "你是一名旅行问卷设计专家。根据输入中的结构化旅行数据（search 字段）、用户画像（user_profile 字段，可能没有）"
    "和基础信息（basic 字段，含目的地/日期/预算/人数等，可能没有），生成一份简短问卷，"
    "目的是帮助后续的行程规划师（PlanAgent）制定更贴合用户喜好的方案。"
    "输出必须是 JSON，结构为："
    '{"questions":[{"id":"q1","type":"single或multiple","question":"问题","options":["选项1","选项2"],"max_select":数字或null}]}。'
    "设计原则："
    "1) 共 4~6 道题，全部为选择题；single 表示单选，multiple 表示多选（多选时 max_select 给数字，单选给 null）；"
    "2) 只问能影响 PlanAgent 决策的问题，不要重复画像里已经明确的偏好；"
    "3) 选项尽量来自 search 数据里的真实名称（景点、酒店、促销等），每道 2~5 个选项；"
    "4) 典型问题：最想去的景点（多选，选项取 search.poi 的景点名）、偏好的酒店（单选，取 search.hotels 的酒店名）、"
    "想尝试的美食类型（多选，结合本地特色与画像 interests）、预算更倾向花在哪（单选：酒店/门票/美食/购物）、"
    "市内出行方式（单选：打车/地铁/公交/自驾）、更偏室内还是户外（结合天气）等；"
    "5) 结合天气、预算、同行人做个性化（带孩子→问亲子偏好；预算有限→问预算侧重；天气有雨→问室内/户外偏好）；"
    "6) 只输出 JSON，不要任何多余文字或代码块。"
)


def build_questionnaire(
    search_result: dict,
    profile: dict | None = None,
    basic: dict | None = None,
) -> dict:
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    context: dict = {"search": search_result}
    if profile:
        context["user_profile"] = profile
    if basic:
        context["basic"] = basic

    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
        messages=[
            {"role": "system", "content": QUESTIONNAIRE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        max_tokens=8000,
        timeout=120,
    )
    content = (resp.choices[0].message.content or "{}").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


def main() -> None:
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
    else:
        path = sys.argv[1] if len(sys.argv) > 1 else ""
        if path and os.path.exists(path):
            raw = Path(path).read_text(encoding="utf-8").strip()
        else:
            raw = " ".join(sys.argv[1:]).strip()

    if not raw:
        print(json.dumps({"error": "empty input"}, ensure_ascii=False))
        return

    try:
        data = json.loads(raw)
        if isinstance(data, dict) and any(k in data for k in ("search", "profile", "basic")):
            search_result = data.get("search") or {}
            profile = data.get("profile")
            basic = data.get("basic")
        else:
            search_result = data
            profile = None
            basic = None
        result = build_questionnaire(search_result, profile, basic)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
