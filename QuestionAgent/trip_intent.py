"""旅行参数校验与按需问卷：已知信息不重复问，可用自由文本补充。"""

import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


REQUIRED_FIELDS = (
    ("destination", "目的地"),
    ("start_date", "出发日期"),
    ("end_date", "返程日期或游玩天数"),
    ("origin", "出发地"),
    ("travelers", "出行人数"),
    ("total_budget", "本次旅行总预算（也可以填不限）"),
)
TEXT_FIELDS = ("destination", "origin", "food_keyword", "notes")
LIST_FIELDS = ("purposes", "budget_tiers", "travel_style")
_UNSET_TEXT = {"", "不确定", "未确定", "还没定", "待定", "暂未确定", "不知道", "清除"}
TRAVEL_STYLES = (
    "自然景观", "历史人文", "主题娱乐", "城市地标与购物", "户外运动与体验",
)


def local_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    text = str(value or "").replace(",", "").replace("，", "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万千]?)\s*(?:元|人民币)?", text)
    if not match:
        return None
    number = float(match[1]) * {"": 1, "千": 1000, "万": 10000}[match[2]]
    return number if math.isfinite(number) and number > 0 else None


def _unset(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() in _UNSET_TEXT) or value == []


def _valid_date(value, today: date) -> date | None:
    try:
        parsed = date.fromisoformat(str(value or ""))
        return parsed if parsed >= today else None
    except ValueError:
        return None


def normalize_trip_data(extracted: dict, previous: dict | None = None, today: date | None = None) -> dict:
    """合并本轮明确的修正；null 表示撤销，不把旧值当作最新事实。"""
    today = today or local_today()
    known = dict(previous or {})
    for key, value in extracted.items():
        if _unset(value):
            known.pop(key, None)
        else:
            known[key] = value

    result = {}
    for key in TEXT_FIELDS:
        if isinstance(known.get(key), str) and not _unset(known[key]):
            result[key] = known[key].strip()
    for key in LIST_FIELDS:
        value = known.get(key)
        if isinstance(value, str):
            value = re.split(r"[,，、]", value)
        if isinstance(value, list):
            values = list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))
            if key == "budget_tiers":
                values = [v for v in values if v in ("经济", "舒适", "豪华", "不设限")]
            if values:
                result[key] = values

    travelers = str(known.get("travelers") or "")
    counts = re.findall(r"(?<![\d.])(\d+)\s*(?:位|个)?(?:成人|大人|孩子|儿童|老人|人|大|小)", travelers)
    count = sum(int(v) for v in counts) if counts else _number(travelers)
    if count and int(count) == count:
        result["travelers"] = f"{int(count)}人"

    # 移动出发日时保留旅行长度；改返程日则重算天数。
    if "duration_days" in extracted and "end_date" not in extracted:
        known.pop("end_date", None)
    if (
        "end_date" in extracted and _unset(extracted["end_date"]) and "duration_days" not in extracted
        and not ("start_date" in extracted and _unset(extracted["start_date"]))
    ):
        known.pop("duration_days", None)
    if "start_date" in extracted and "end_date" not in extracted and known.get("duration_days"):
        known.pop("end_date", None)
    duration = _number(known.get("duration_days"))
    if duration and int(duration) == duration and duration <= 365:
        result["duration_days"] = int(duration)
    start = _valid_date(known.get("start_date"), today)
    end = _valid_date(known.get("end_date"), today)
    if start:
        result["start_date"] = start.isoformat()
    if end and (not start or end >= start):
        result["end_date"] = end.isoformat()
        if start:
            result["duration_days"] = (end - start).days + 1
    elif start and result.get("duration_days") and "end_date" not in extracted:
        result["end_date"] = (start + timedelta(days=result["duration_days"] - 1)).isoformat()

    # 切换成总额后丢弃旧人均值，防止改人数时重算用户明确的总预算。
    unlimited_values = ("不限", "不设限", "无上限")
    if extracted.get("budget_unlimited") is True or extracted.get("total_budget") in unlimited_values:
        known["budget_mode"] = "unlimited"
        known["budget_unlimited"] = True
        known.pop("total_budget", None)
        known.pop("budget_per_person", None)
    elif _number(extracted.get("total_budget")) or ("total_budget" in extracted and "budget_per_person" not in extracted):
        known.pop("budget_per_person", None)
        known.pop("budget_unlimited", None)
        known["budget_mode"] = "total"
    elif "budget_per_person" in extracted:
        known.pop("total_budget", None)
        known.pop("budget_unlimited", None)
        known["budget_mode"] = "per_person"
    elif "budget_unlimited" in extracted and extracted["budget_unlimited"] is not True:
        known.pop("budget_unlimited", None)
        if known.get("budget_mode") == "unlimited":
            known.pop("budget_mode", None)
    mode = known.get("budget_mode")
    if mode not in ("total", "per_person", "unlimited"):
        mode = "unlimited" if known.get("budget_unlimited") is True else "per_person" if _number(known.get("budget_per_person")) else "total"
    if mode == "unlimited" and known.get("budget_unlimited") is True:
        result.update(budget_unlimited=True, budget_mode="unlimited", budget_tiers=["不设限"])
    elif mode == "per_person":
        per_person = _number(known.get("budget_per_person"))
        if per_person:
            result.update(budget_per_person=per_person, budget_mode="per_person")
            if result.get("travelers"):
                result["total_budget"] = per_person * int(count)
    else:
        total = _number(known.get("total_budget"))
        if total:
            result.update(total_budget=total, budget_mode="total")
    if not result.get("budget_unlimited") and result.get("budget_tiers") == ["不设限"]:
        result.pop("budget_tiers")
    return result


def _fallback_question(field: str, data: dict, today: date) -> dict:
    destination = data.get("destination")
    if field == "destination":
        return {"field": field, "question": "这次想去哪个城市或地区？", "options": []}
    if field == "start_date":
        tomorrow = today + timedelta(days=1)
        saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
        return {"field": field, "question": f"计划什么时候出发{'去' + destination if destination else ''}？", "options": [f"明天（{tomorrow.isoformat()}）", f"周末（{saturday.isoformat()}）", f"下周末（{(saturday + timedelta(days=7)).isoformat()}）"]}
    if field == "end_date":
        return {"field": field, "question": "计划玩几天，或者哪天返程？", "options": ["2天", "3天", "5天"]}
    if field == "origin":
        return {"field": field, "question": "从哪个城市出发？", "options": []}
    if field == "travelers":
        return {"field": field, "question": "这次共有几个人出行？成人和孩子都要算上。", "options": ["1人", "2人", "3人", "4人"]}
    if field == "travel_style":
        return {"field": field, "question": "这次旅行想走什么风格？", "options": list(TRAVEL_STYLES)}
    people = _number(str(data.get("travelers", "")).removesuffix("人")) or 1
    days = data.get("duration_days") or 3
    base = int(math.ceil(people * days * 500 / 500) * 500)
    return {"field": field, "question": "这次旅行的预算是多少？可以填所有人的总预算、每人预算，或不限。", "options": [f"总预算{base}元", f"总预算{base * 2}元", "预算不限"]}


def _questions(missing: list[str], data: dict, suggestions, today: date) -> list[dict]:
    # 模型只负责措辞和候选，实际缺项由代码决定。
    by_field = {}
    for item in suggestions if isinstance(suggestions, list) else []:
        if not isinstance(item, dict) or item.get("field") not in missing:
            continue
        field = item["field"]
        if field in by_field:
            continue
        question = item.get("question")
        options = item.get("options")
        if not isinstance(question, str) or not question.strip():
            continue
        clean_options = list(dict.fromkeys(v.strip() for v in options if isinstance(v, str) and v.strip()))[:4] if isinstance(options, list) else []
        by_field[field] = {"field": field, "question": question.strip()[:200], "options": clean_options}
    return [by_field.get(field) or _fallback_question(field, data, today) for field in missing[:3]]


def complete_trip_request(extracted: dict, previous: dict | None = None, today: date | None = None, questions=None) -> dict:
    today = today or local_today()
    data = normalize_trip_data(extracted, previous, today)
    missing = [
        field for field, _ in REQUIRED_FIELDS
        if not data.get(field)
        and not (field == "total_budget" and (data.get("budget_unlimited") or data.get("budget_per_person")))
        and not (field == "end_date" and not data.get("start_date") and data.get("duration_days"))
    ]
    if not missing:
        return {"action": "confirm_trip", "data": data}
    qs = _questions(missing, data, questions, today)
    if not data.get("travel_style"):
        qs.append(_fallback_question("travel_style", data, today))
    return {
        "action": "ask",
        "field": "trip_details",
        "missing": missing,
        "data": data,
        "question": "补充下面必要的信息就可以开始规划。选一个建议，或直接填写；也可以在聊天框一次说完整。",
        "questions": qs,
        "options": [],
    }
