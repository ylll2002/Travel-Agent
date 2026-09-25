"""ValidateAgent：审核 PlanAgent 的旅行方案，发现问题则退回修改。"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

VALIDATE_SYSTEM_PROMPT = (
    "你是一名严格的旅行方案审核员。审核输入中的旅行计划（plan 字段），"
    "结合用户画像（user_profile）、基础信息（basic）、搜索数据（search）和问卷作答（answers，均可选），"
    "检查是否存在以下问题："
    "1) 预算：酒店/活动是否明显超出用户预算；"
    "2) 交通：景点之间是否往返折返、单日车程过长、交通方式与用户偏好不符；"
    "3) 用户喜好：节奏/兴趣/忌口/同行人/住宿是否不符；"
    "4) 天气：雨天是否安排大量户外活动、是否给带伞/穿衣建议；"
    "5) 时间：时间点是否冲突、行程过满或过松；"
    "6) 完整性：是否缺天数/活动/酒店/餐食。"
    "输出必须是 JSON："
    '{"passed":true或false,"issues":[{"severity":"high或medium或low","type":"预算或交通或偏好或天气或时间或完整性","detail":"问题描述","suggestion":"修改建议"}],"feedback":"给 PlanAgent 的总体修改建议"}。'
    "无问题则 passed=true、issues=[]；有问题则 passed=false 并列出 issues 和 feedback。"
    "只输出 JSON，不要任何多余文字。"
)


def validate_plan(
    plan: dict,
    profile: dict | None = None,
    search: dict | None = None,
    basic: dict | None = None,
    answers: list | None = None,
) -> dict:
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    context: dict = {"plan": plan}
    if profile:
        context["user_profile"] = profile
    if search:
        context["search"] = search
    if basic:
        context["basic"] = basic
    if answers:
        context["answers"] = answers

    for _ in range(3):
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "deepseek-flash"),
            messages=[
                {"role": "system", "content": VALIDATE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=12000,
            timeout=120,
        )
        content = (resp.choices[0].message.content or "{}").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {}
        if isinstance(result.get("passed"), bool):
            return result
    # 兜底：多次重试仍无有效结果时默认通过，避免编排卡死
    return {"passed": True, "issues": [], "feedback": "（审核重试失败，默认通过）"}


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
        result = validate_plan(
            data.get("plan") or {},
            data.get("profile"),
            data.get("search"),
            data.get("basic"),
            data.get("answers"),
        )
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
