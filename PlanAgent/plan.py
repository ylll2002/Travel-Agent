"""PlanAgent：读取 SearchAgent 的结构化输出 + 用户画像，生成逐日旅行计划。

用法：
  # 先跑 SearchAgent 拿到结构化结果，再喂给 PlanAgent
  echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03"}' \
    | python ../SearchAgent/search.py \
    | python plan.py

输入（三选一）：
  1) SearchAgent 返回的 JSON（weather/hotels/poi/promotions），不带画像/作答
  2) {"profile": {...用户画像...}, "search": {...搜索结果...}}
  3) {"profile": {...}, "search": {...}, "answers": [{"question":"...","answer":"..."}]}
输出：旅行计划 JSON（含逐日 itinerary）
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PLAN_SYSTEM_PROMPT = (
    "你是一名专业的旅行规划师。根据输入中的结构化旅行数据（search 字段）和用户画像（user_profile 字段，可能没有），"
    "生成两个不同风格的旅行计划，供用户选择。两个计划都必须符合用户画像的偏好（预算、同行人、节奏、兴趣、忌口等）。"
    "两个风格固定为：1) 经典人气（热门必打卡）；2) 小众深度（小众深度体验）。"
    "输出必须是 JSON，结构如下："
    '{"destination":"目的地","start_date":"开始日期","end_date":"结束日期","days":天数,"weather_summary":"天气摘要",'
    '"plans":['
    '{"style":"经典人气","summary":"一句话概述","itinerary":['
    '{"day":1,"date":"日期","theme":"当天主题","schedule":['
    '{"time":"09:00-11:30","type":"景点","name":"活动名","note":"说明（含与上一点的交通方式和耗时）"}'
    '],"hotel":"推荐酒店","meals":[{"meal":"午餐","options":["饭店1","饭店2"]}],"tips":"当天提示"}'
    '],"recommended_hotels":["..."],"recommended_promotions":["..."],"notes":"..."},'
    '{"style":"小众深度","summary":"一句话概述","itinerary":[...],"recommended_hotels":[...],"recommended_promotions":[...],"notes":"..."}'
    ']}。'
    "如果输入中包含 user_profile（用户画像），其字段含义为："
    "age_group=年龄段；mbti=MBTI人格；city=常住城市；companion=同行人（可多选：独自/伴侣/带孩子/带老人/朋友/同事）；"
    "pace=旅行节奏（慢节奏深度游/适中/紧凑打卡）；budget=预算偏好（经济实惠/舒适型/豪华型/不设限）；"
    "accommodation=住宿偏好（酒店/民宿/客栈）；transport=交通偏好（高铁/飞机/自驾）；"
    "interests=兴趣（自然风光/人文历史/主题乐园/博物馆/美食购物/户外运动/温泉度假/摄影）；"
    "dietary=饮食忌口（海鲜过敏/不吃辣/素食/清真）。"
    "如果输入中包含 answers（用户对问卷的作答，每项含 question 和 answer），请优先严格遵循用户的选择来规划"
    "（已选的景点、酒店、出行方式、预算侧重等），未作答的项再按画像和常识默认。"
    "如果输入中包含 feedback（上次审核的修改建议），必须据此修正计划中列出的问题。"
    "如果输入中包含 modify（含 block_id 和 instruction），按 instruction 修改对应那一个活动块，其余块尽量保持不变。"
    "个性化与规划规则："
    "1) 时间点：每个活动给出精确起止时间（HH:MM-HH:MM），考虑景点开放时间与用餐时间，前后衔接合理；"
    "2) 节奏：pace 含「紧凑打卡」→每天4~5个活动；「适中」→3~4个；「慢节奏深度游」→2~3个；"
    "3) 美食：若 interests 含「美食/美食购物」，每餐提供2~3家饭店备选（写入 meals.options，并在 schedule 的餐食 note 里列出）；否则每餐1家即可；"
    "4) 交通：相邻活动之间在 note 里注明交通方式与大致耗时（打车/地铁/步行/自驾），尽量地理就近、减少折返；"
    "5) 忌口：dietary 的限制贯彻到每一餐；结合天气给穿衣/带伞建议；"
    "6) 优先使用数据中真实存在的酒店和景点名称；两个方案风格差异明显；"
    "7) schedule 每项必须带 type，取值：交通/美食/景点/酒店/活动；只输出 JSON，不要任何多余文字。"
)


def build_plan(
    search_result: dict,
    profile: dict | None = None,
    answers: list | None = None,
    feedback: str | None = None,
    modify: dict | None = None,
) -> dict:
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    context: dict = {"search": search_result}
    if profile:
        context["user_profile"] = profile
    if answers:
        context["answers"] = answers
    if feedback:
        context["feedback"] = feedback
    if modify:
        context["modify"] = modify

    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "deepseek-v4-pro"),
        messages=[
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        max_tokens=16000,
        timeout=300,
    )
    content = (resp.choices[0].message.content or "{}").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


TYPE_KEYWORDS = (
    ("交通", ["交通", "地铁", "打车", "乘车", "前往", "返回", "接送", "出发", "抵达", "步行", "自驾", "专线", "换乘", "车站", "车程", "高铁"]),
    ("美食", ["早餐", "午餐", "晚餐", "餐厅", "饭店", "美食", "小吃", "菜馆", "面馆"]),
    ("酒店", ["入住", "退房", "住宿", "酒店", "民宿", "客栈"]),
    ("景点", ["博物院", "博物馆", "公园", "乐园", "景区", "广场", "老街", "外滩", "湿地", "寺", "塔", "影视城", "动物园", "海洋", "湖", "阁", "书院", "山"]),
)


def _infer_type(name: str, note: str) -> str:
    text = f"{name} {note}"
    for typ, keywords in TYPE_KEYWORDS:
        if any(k in text for k in keywords):
            return typ
    return "活动"


def blockify(plan: dict) -> list[dict]:
    """把嵌套的 plans[].itinerary[].schedule[] 拍平成扁平的 blocks 列表。"""
    blocks: list[dict] = []
    counter = 0
    for p in plan.get("plans") or []:
        style = p.get("style") or ""
        for it in p.get("itinerary") or []:
            day = it.get("day")
            date_ = it.get("date") or ""
            has_hotel_block = False
            for item in it.get("schedule") or []:
                counter += 1
                name = item.get("name") or ""
                note = item.get("note") or ""
                typ = item.get("type") or _infer_type(name, note)
                if typ == "酒店":
                    has_hotel_block = True
                blocks.append(
                    {
                        "id": f"b{counter}",
                        "plan_style": style,
                        "day": day,
                        "date": date_,
                        "type": typ,
                        "time": item.get("time") or "",
                        "name": name,
                        "note": note,
                    }
                )
            for meal in it.get("meals") or []:
                counter += 1
                meal_name = meal.get("meal") or ""
                options = [str(o) for o in (meal.get("options") or [])]
                blocks.append(
                    {
                        "id": f"b{counter}",
                        "plan_style": style,
                        "day": day,
                        "date": date_,
                        "type": "美食",
                        "time": meal_name,
                        "name": " / ".join(options) if options else meal_name,
                        "note": "餐饮推荐",
                    }
                )
            hotel = it.get("hotel")
            if hotel and not has_hotel_block:
                counter += 1
                blocks.append(
                    {
                        "id": f"b{counter}",
                        "plan_style": style,
                        "day": day,
                        "date": date_,
                        "type": "酒店",
                        "time": "住宿",
                        "name": hotel,
                        "note": "推荐住宿",
                    }
                )
    return blocks


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
        if isinstance(data, dict) and any(k in data for k in ("search", "profile", "answers", "feedback", "modify")):
            search_result = data.get("search") or {}
            profile = data.get("profile")
            answers = data.get("answers")
            feedback = data.get("feedback")
            modify = data.get("modify")
        else:
            search_result = data
            profile = None
            answers = None
            feedback = None
            modify = None
        plan = build_plan(search_result, profile, answers, feedback, modify)
        if isinstance(plan, dict) and "error" not in plan:
            plan["blocks"] = blockify(plan)
    except Exception as exc:  # noqa: BLE001
        plan = {"error": str(exc)}

    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
