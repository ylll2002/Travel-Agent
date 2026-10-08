"""QuestionAgent：补全旅行信息 + 判断修改意图。

输入（stdin JSON）：
  {"messages":[{"role":"user|assistant","content":"..."}], "has_plan": false, "trip_data": {...}}

输出（stdout JSON）：
  {"action":"ask","missing":[...],"data":{...},"question":"...","questions":[{"field":"start_date","question":"...","options":["..."]}]}
  {"action":"confirm_trip","data":{destination,start_date,end_date,origin,travelers,total_budget,...}}
  {"action":"modify","mode":"global|block","targets":[...],"instruction":"..."}
"""

import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from trip_intent import complete_trip_request, local_today

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
from shared.model_config import MISSING_MODEL_API_KEY, model_api_key_configured

load_dotenv(BASE_DIR / ".env")
# 共用规划模型配置；当前项目没有单独的 QuestionAgent/.env。
load_dotenv(BASE_DIR.parent / "PlanAgent" / ".env")

SYSTEM_PROMPT = (
    "你是旅行需求识别助手。阅读完整对话和 trip_data 中已经提取的信息，识别用户的自由输入，输出 JSON。\n"
    "1) 用户想规划旅行或补充旅行信息时，输出 action=collect，data 为已知旅行参数对象。"
    "用户可以一次提供多项信息，也可以只补充一项；提取本轮用户明确表达或修正的参数，最新修正覆盖旧信息。"
    "data 是本轮变更，不要重复输出 trip_data 中没有修改的字段，不要把助手的问卷选项当作用户已选择。"
    "字段包括 destination(具体目的地城市)、origin(出发城市)、start_date/end_date(YYYY-MM-DD)、"
    "duration_days(游玩天数，整数)、travelers(总人数，如3人)、total_budget(全体出行者本次旅行总预算，元)、"
    "budget_per_person(明确为每人整趟预算时填写，元)、budget_unlimited(明确不限制预算时为true)、"
    "budget_tiers(可选，经济/舒适/豪华/不设限数组)、purposes(兴趣/旅行目的数组)、"
    "requested_pois(用户点名想去的景点数组)、"
    "travel_style(本次旅行风格数组，如休闲度假/深度文化/自然风光/美食探店/亲子乐园/购物血拼/冒险户外/摄影旅拍)、"
    "food_keyword(明确的菜系或餐厅关键词)、"
    "notes(交通、住宿、节奏、饮食忌口等其他要求)。"
    "用户点名想去的地点必须写入 requested_pois，例如'想看看西湖'要提取 requested_pois=[\"西湖\"]，"
    "不能仅把具体地点放进 purposes；purposes 用于美食、亲子、自然风景等兴趣类型。"
    "例如两大一小总人数为3人，预算一万为10000元；必须区别总预算和每人预算。"
    "相对日期如明天、下周五根据提供的今天日期换算；只输出用户明确给出的出发日期、返程日期或天数，派生日期由程序计算。"
    "人均预算只输出 budget_per_person，不同时输出派生总额；总预算只输出 total_budget。"
    "用户明确说日期/预算/目的地等还没定、撤销或不确定时，将对应字段输出为 null，不能沿用原值。"
    "出发时间还没定时清除 start_date，保留已知游玩天数；游玩天数还没定时清除 duration_days；"
    "只说返程时间不确定时清除 end_date 和 duration_days。"
    "清除预算时输出 total_budget=null；取消预算不限时输出 budget_unlimited=false；"
    "改出发日期时不要保留旧返程日期，除非本轮也明确给出了新的返程日。"
    "notes 中保留仍有效的饮食、交通、节奏等要求，新增要求合并进去，明确取消的要求删除。"
    "仅说玩几天但没说何时出发，不得默认为今天；没说返程日期或天数不得默认三天。"
    "没有表达的信息省略，严禁自行假定目的地、日期、人数、金额。预算档位和兴趣均为可选，不要主动追问。"
    "只从用户消息和 trip_data 提取事实，助手的举例或建议不代表用户已选择。"
    "合并本轮 data 与 trip_data 后，如果缺少规划必要信息，可同时输出 questions 数组，最多3题。"
    '每题结构为 {"field":"字段名","question":"与已知旅行信息相关的简短问题","options":["可选答案"]}。'
    "只问仍缺失的 destination、start_date、end_date（返程日或天数）、origin、travelers、total_budget；"
    "有游玩天数时不要再问返程日；人均预算已知但人数未知时只需追问人数，不重复问预算。"
    "优先问缺少的目的地、出发日期、游玩天数，然后出发地、人数、预算。"
    "按已知目的地、时长和人数提供2至4个自然且具体的候选，也允许 options=[] 让用户直接输入；"
    "日期选项用未来具体日期，预算选项必须明确总额或人均，并可包含预算不限。"
    "不要问已知字段、兴趣、预算档位、交通方式或餐饮偏好等非必要问题。问卷建议不写入 data。"
    "如果已有计划但用户明确要另起一次新旅行（如另开一个新行程、重新开始规划），输出 new_trip=true，"
    "data 只提取这次新旅行的信息。仅修改选中的行程、日期、预算或目的地不代表另一次新旅行，应按修改处理。\n"
    "2) 如果 has_plan=true 且用户是在修改已有方案，先归纳修改意图，输出："
    '{"action":"confirm","summary":"我理解你是想……","mode":"global|block","targets":[...],"instruction":"用户原意"}。'
    "具体景点/酒店/活动→block；整体意见→global。"
    "明确说删除、删了、去掉或不去某条行程，表示移除该条目并保留空档，不得改成替换、另找或补入其他景点；instruction 保留用户的删除原意。"
    "当用户在后续对话中明确确认（例如「对」「可以」「确认」）时，输出："
    '{"action":"modify","mode":"...","targets":[...],"instruction":"..."}。'
    "当用户否认或补充（例如「不对，应该是……」）时，继续输出 action=confirm 更新归纳。"
    "3) 如果用户只是询问某个地点/景点的介绍（例如「宽窄巷子是玩啥的」「这个景点怎么样」），"
    '输出 {"action":"explain","answer":"简短解释","query":"宽窄巷子"}，不要规划行程。'
    "只输出 JSON，不要任何多余文字。"
)


def _intent_error(message: str, trip_data: dict) -> dict:
    return {"error": message, "data": deepcopy(trip_data)}


def _parse_intent_response(content: str) -> dict:
    if not isinstance(content, str):
        raise ValueError("响应内容必须是JSON文本")
    content = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL)
    if fenced:
        content = fenced[1]
    result = json.loads(content)
    # Qwen sometimes wraps its only response object in an array. A unique
    # object is unambiguous; never choose one item from multiple responses.
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0]
    if not isinstance(result, dict):
        raise ValueError("顶层必须是唯一JSON对象，不得输出空数组或多个结果")
    action = result.get("action")
    if action in ("collect", "plan", "ask"):
        if not isinstance(result.get("data"), dict):
            raise ValueError("collect/plan/ask必须含data对象，不得用数组、文本或null代替")
        if "new_trip" in result and not isinstance(result["new_trip"], bool):
            raise ValueError("new_trip如提供必须是布尔值")
    elif action == "explain":
        if not isinstance(result.get("answer"), str) or not result["answer"].strip():
            raise ValueError("explain必须提供非空answer文本")
        if "query" in result and not isinstance(result["query"], str):
            raise ValueError("query如提供必须是文本")
    elif action in ("confirm", "modify"):
        if not isinstance(result.get("instruction"), str) or not result["instruction"].strip():
            raise ValueError("confirm/modify必须提供非空instruction文本")
        if "mode" in result and result["mode"] not in ("global", "block"):
            raise ValueError("mode只能为global或block")
        if "targets" in result and not isinstance(result["targets"], list):
            raise ValueError("targets如提供必须是数组")
        if "summary" in result and not isinstance(result["summary"], str):
            raise ValueError("summary如提供必须是文本")
    else:
        raise ValueError("action只能为collect、plan、ask、explain、confirm或modify")
    return result


def resolve_intent(data: dict, client: OpenAI | None = None) -> dict:
    if not isinstance(data, dict):
        return _intent_error("请输入有效的旅行需求", {})
    messages = data.get("messages") or []
    has_plan = bool(data.get("has_plan"))
    trip_data = data.get("trip_data") if isinstance(data.get("trip_data"), dict) else {}
    today = local_today()
    if not messages:
        return _intent_error("请输入旅行需求", trip_data)

    if client is None:
        if not model_api_key_configured():
            return _intent_error(MISSING_MODEL_API_KEY + "已填写的信息已保留。", trip_data)
        kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY"), "max_retries": 0}
        if os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
        try:
            client = OpenAI(**kwargs)
        except Exception:
            return _intent_error("需求识别暂时不可用，请稍后重试；已填写的信息已保留", trip_data)
    else:
        # A supplied real SDK client must obey the same bounded retry policy.
        try:
            client.max_retries = 0
        except (AttributeError, TypeError):
            pass

    model = os.getenv("OPENAI_MODEL", "qwen3.8-27b")
    model_options = {"extra_body": {"enable_thinking": False}} if model.lower().startswith("qwen") else {}
    request_messages = [
        {"role": "system", "content": f"今天是 {today.isoformat()}。\n{SYSTEM_PROMPT}"},
        {"role": "user", "content": json.dumps(
            {"messages": messages, "has_plan": has_plan, "trip_data": trip_data}, ensure_ascii=False)},
    ]
    result = None
    for attempt in range(2):
        content = ""
        try:
            resp = client.chat.completions.create(
                model=model, messages=deepcopy(request_messages),
                response_format={"type": "json_object"}, max_tokens=2500,
                timeout=30, **model_options,
            )
        except Exception as exc:
            message = "需求识别超时，请重试；已填写的信息已保留" if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__ else "需求识别暂时不可用，请稍后重试；已填写的信息已保留"
            return _intent_error(message, trip_data)
        try:
            content = resp.choices[0].message.content or ""
            result = _parse_intent_response(content)
            break
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            if attempt:
                return _intent_error("未能正确识别这条需求，请重试或直接补充旅行信息；已填写的信息已保留", trip_data)
            content = content if isinstance(content, str) else ""
            request_messages.extend([
                {"role": "assistant", "content": content},
                {"role": "user", "content":
                 "请纠正上一条响应的结构错误：" + str(exc) + "。"
                 "仅返回一个JSON对象，不要数组、Markdown或说明文字。"
                 "action仅允许collect/plan/ask/explain/confirm/modify。"
                 "旅行信息提取用collect，data必须为对象，只含原始用户消息明确表达的本轮参数；"
                 "不要补造日期、预算、人数、目的地，也不要把纠正格式视为新旅行。"
                 "解释用explain并提供answer；修改用confirm/modify并提供instruction，mode只可global/block。"
                 "依据上面的原始对话和trip_data重新输出，保留已经填写的信息。"},
            ])
    if result is None:
        return _intent_error("需求识别暂时不可用，请重试；已填写的信息已保留", trip_data)
    if result.get("action") in ("collect", "plan", "ask"):
        extracted = result.get("data")
        previous = {} if result.get("new_trip") is True else trip_data
        return complete_trip_request(extracted, previous, today, questions=result.get("questions"))
    if result.get("action") in ("explain", "confirm", "modify"):
        return result
    return _intent_error("未能识别这条需求，请重新描述；已填写的信息已保留", trip_data)


def main() -> None:
    try:
        raw = sys.stdin.read().strip()
        result = resolve_intent(json.loads(raw)) if raw else {"error": "empty input"}
    except Exception:  # noqa: BLE001
        result = {"error": "需求识别暂时不可用，请重新描述旅行需求"}

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
