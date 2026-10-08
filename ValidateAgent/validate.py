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
    "你是一名旅行方案审核员。根据 plan、用户明确要求 basic、用户画像 user_profile、"
    "长期偏好 preferences、历史行程 recent_trips 和来源 search，审核计划是否可以执行。"
    "只依据输入中的证据，不凭空推断价格、营业时间、交通或用户要求。"
    "预算以用户明确的总预算或专项硬上限为准：plan.cost_by_style 是每个独立方案的已知费用估算，"
    "不同方案不能相加。block.price 已按 price_basis 和人数计入总费用，不得再次乘人数；"
    "unit_price 或来源 price_per_person 才是人均费用。"
    "meal_budget、over_meal_budget、餐饮占总预算25%、平均每天/每餐分摊等均为系统排序的软建议，"
    "除非 basic 明确规定对应餐饮上限，否则超出这种分摊不等于违反用户预算。"
    "休闲、轻松、度假只代表节奏偏好，不能推断为必须低消费或禁止某个价位的餐厅。"
    "price_known=false、unit_price=null、unpriced_items 中的项目是报价未知，显示price=0也不代表免费。"
    "已知小计超过用户硬总预算才可据此认定超预算；如果已知小计低于预算但酒店/交通等报价未知，"
    "应说明预算状态unknown、预订前核实，不得断言整体超预算，也不得断言总费用一定满足预算。"
    "重复餐厅、价位分布或性价比可以作为low/medium优化建议，不能仅据此阻止计划通过。"
    "交通与时间：结合真实legs的distance_m/duration_s、行程日期和前后活动核对能否赶到；"
    "跨日交通不能当成同一天的时间冲突。休闲需求允许自由活动、休息、候车与留白，"
    "有几小时空档本身不是冲突，也不应为了填满而增加活动。"
    "景点关闭、预约不可用、活动重叠或无法赶上已选返程班次等有证据的执行冲突属于high；"
    "仅营业信息不明、夜市最佳游览时段、缺少穿衣建议等通常是待核实或优化建议。"
    "同时检查用户明确的忌口、同行人需求、完整天数、必要交通和住宿，"
    "天气仅依据search已有天气证据，不把预报缺失等同恶劣天气。"
    "severity=high仅用于有证据、影响实际执行或违反用户明确硬约束的严重问题；"
    "medium/low用于不会使计划无法执行的风险提示与改进。actionable=true表示PlanAgent可以通过"
    "调整已有行程或已有真实候选修复；仅等待第三方报价/核实信息时actionable=false。"
    "passed=false用于存在影响执行的high问题；没有这种严重问题时passed=true，"
    "仍保留真实的medium/low建议和未知报价说明，不要求issues为空。"
    "输出JSON："
    '{"passed":true或false,"issues":[{"severity":"high或medium或low","type":"预算或交通或偏好或天气或时间或完整性",'
    '"detail":"有证据的问题描述","suggestion":"具体修正或核实建议","actionable":true或false}],'
    '"feedback":"给PlanAgent的总体修改建议；没有严重问题时说明优化建议不影响执行"}。'
    "只有真的无问题时issues=[]。只输出JSON，不要额外文字。"
)


def _failed_audit(error: str, feedback: str) -> dict:
    return {"passed": False, "issues": [], "feedback": feedback, "error": error}


def validate_plan(
    plan: dict,
    profile: dict | None = None,
    preferences: dict | None = None,
    recent_trips: list | None = None,
    search: dict | None = None,
    basic: dict | None = None,
) -> dict:
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    try:
        client = OpenAI(**kwargs, max_retries=0)
    except Exception:
        return _failed_audit("审核服务不可用", "审核初始化失败，请检查模型配置后重试")

    context: dict = {"plan": plan}
    if profile:
        context["user_profile"] = profile
    if preferences:
        context["preferences"] = preferences
    if recent_trips:
        context["recent_trips"] = recent_trips
    if search:
        context["search"] = search
    if basic:
        context["basic"] = basic
    for _ in range(2):
        try:
            model = os.getenv("OPENAI_MODEL", "deepseek-flash")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": VALIDATE_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                # 推理型模型（qwen / deepseek）关闭思考可减少 reasoning 占用；
                # 注意这不是万能药，仍需为 max_tokens 留足预算（见下方说明）。
                **({"extra_body": {"enable_thinking": False}}
                   if model.lower().startswith(("qwen", "deepseek")) else {}),
                # 推理模型与 content 共享该预算：实测 2400 会被 reasoning 吃满、
                # content 为空导致审核结果无效（编排层只能降级为"审核未完成"）。
                # 12000 可稳定留出 content 空间。
                max_tokens=12000,
                timeout=120,
            )
        except Exception:
            return _failed_audit("审核服务不可用", "审核调用失败，请稍后重试")
        try:
            content = (resp.choices[0].message.content or "{}").strip()
        except (AttributeError, IndexError, TypeError):
            continue
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {}
        if (isinstance(result, dict) and isinstance(result.get("passed"), bool)
                and isinstance(result.get("issues", []), list) and not result.get("error")):
            return result
    return _failed_audit("审核结果不可用", "审核未返回有效结论")


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
            data.get("preferences"),
            data.get("recent_trips"),
            data.get("search"),
            data.get("basic"),
        )
    except Exception:  # noqa: BLE001
        result = _failed_audit("审核失败", "审核未能完成，请稍后重试")

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
