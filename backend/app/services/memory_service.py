"""行为信号 → 长期偏好 的规则聚合器。

把 BehaviorSignal 原始流水归纳成 UserPreference.preferences。
规则是可解释的确定性规则：同类正向/负向信号达到阈值后才升级为偏好。
"""

from copy import deepcopy
import hashlib
import json
import os
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BehaviorSignal, MemoryCache, TripMemory, UserPreference
import sys

ROOT = Path(__file__).resolve().parents[3]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_env import component_python
CONVERSATION_SUMMARY_PY = ROOT / "PlanAgent" / "conversation_summary.py"
PLAN_SUMMARY_PY = ROOT / "PlanAgent" / "plan_summary.py"
TRIP_SUMMARY_PY = ROOT / "PlanAgent" / "trip_summary.py"
PLAN_PYTHON = component_python("PlanAgent")

POSITIVE_ACTIONS = {
    "add",
    "like",
    "keep",
    "prefer",
    "select",
    "confirm",
}
NEGATIVE_ACTIONS = {
    "remove",
    "dislike",
    "skip",
    "drop",
    "avoid",
    "delete",
}

PACE_WORDS = {
    "轻松": ["轻松", "休闲", "慢节奏", "不赶", "宽松"],
    "紧凑": ["紧凑", "充实", "多玩", "快节奏", "排满"],
}
TRANSPORT_WORDS = {
    "打车优先": ["打车", "出租车", "网约车", "taxi"],
    "地铁优先": ["地铁", "轨道交通", "subway", "metro"],
    "公交优先": ["公交", "巴士", "bus"],
    "自驾优先": ["自驾", "租车", "开车"],
}
HOTEL_MUST = ["含早", "早餐", "近地铁", "高楼层", "安静", "有窗", "湖景", "海景"]
HOTEL_AVOID = ["无窗", "临街", "隔音差", "太吵"]
FOOD_AVOID = ["不吃辣", "太辣", "忌口", "不吃香菜", "不吃葱", "不吃蒜"]
FOOD_CUISINES = [
    "川菜", "湘菜", "粤菜", "徽菜", "浙菜", "杭帮菜", "本帮菜", "淮扬菜", "鲁菜",
    "火锅", "烧烤", "烤鱼", "小龙虾", "日料", "日式", "韩料", "西餐", "法餐", "意餐",
    "面食", "面条", "拉面", "海鲜", "素食", "清真", "甜品", "咖啡", "早茶", "自助",
    "饺子", "包子", "生煎", "小吃", "麻辣", "清淡",
]


def _action_polarity(action: str) -> int:
    a = (action or "").strip().lower()
    if a in POSITIVE_ACTIONS or any(k in a for k in ("add", "like", "keep", "prefer", "select", "confirm")):
        return 1
    if a in NEGATIVE_ACTIONS or any(k in a for k in ("remove", "dislike", "skip", "drop", "avoid", "delete")):
        return -1
    return 0


def _count_hits(signals: list[BehaviorSignal], keywords: list[str], polarity: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in signals:
        if _action_polarity(s.action) != polarity:
            continue
        text = f"{s.target} {s.detail}".lower()
        for kw in keywords:
            if kw.lower() in text:
                counts[kw] = counts.get(kw, 0) + 1
    return counts


def _preferred(signals: list[BehaviorSignal], groups: dict[str, list[str]]) -> list[str]:
    """从多组关键词中选出累计出现次数最多的一组，平票取先出现者。"""
    scores: list[tuple[int, int, str]] = []
    for idx, (label, words) in enumerate(groups.items()):
        score = 0
        for s in signals:
            text = f"{s.target} {s.detail}".lower()
            if any(w.lower() in text for w in words):
                score += 1
        scores.append((score, -idx, label))
    scores.sort(reverse=True)
    if not scores or scores[0][0] == 0:
        return []
    return [scores[0][2]]


def _preferred_all(signals: list[BehaviorSignal], groups: dict[str, list[str]]) -> list[str]:
    """返回所有出现过的偏好标签，按出现次数降序，用于可多选的维度（如交通）。"""
    scores: list[tuple[int, int, str]] = []
    for idx, (label, words) in enumerate(groups.items()):
        score = 0
        for s in signals:
            text = f"{s.target} {s.detail}".lower()
            if any(w.lower() in text for w in words):
                score += 1
        scores.append((score, -idx, label))
    scores.sort(reverse=True)
    return [label for score, _, label in scores if score > 0]


def _learn(signals: list[BehaviorSignal], min_count: int) -> dict:
    prefs: dict = {}

    # 交通：直接取出现最多的偏好（节奏 pace 改为只从历史行程提取）
    transport = _preferred_all(signals, TRANSPORT_WORDS)
    if transport:
        prefs["transport"] = transport

    # 酒店硬性要求 / 避雷，餐饮口味 / 忌口 / 喜欢的菜系
    hotel_must = [k for k, c in _count_hits(signals, HOTEL_MUST, 1).items() if c >= min_count]
    hotel_avoid = [k for k, c in _count_hits(signals, HOTEL_AVOID, -1).items() if c >= min_count]
    food_avoid = [k for k, c in _count_hits(signals, FOOD_AVOID, -1).items() if c >= min_count]
    food_cuisines = [k for k, c in _count_hits(signals, FOOD_CUISINES, 1).items() if c >= min_count]
    if hotel_must:
        prefs.setdefault("hotel", {})["must"] = hotel_must
    if hotel_avoid:
        prefs.setdefault("hotel", {})["avoid"] = hotel_avoid
    if food_cuisines:
        prefs.setdefault("food", {})["cuisines"] = food_cuisines
    if food_avoid:
        prefs.setdefault("food", {})["avoid"] = food_avoid

    # 避雷对象（喜欢 liked 改为从最近高评价行程确定性提取）
    disliked: dict[str, int] = {}
    for s in signals:
        target = (s.target or "").strip()
        if not target:
            continue
        polarity = _action_polarity(s.action)
        if polarity < 0:
            disliked[target] = disliked.get(target, 0) + 1
    disliked = {k: v for k, v in disliked.items() if v >= min_count}
    if disliked:
        prefs["avoided"] = sorted(disliked, key=lambda k: -disliked[k])

    return prefs


def _merge(base: dict, learned: dict) -> dict:
    """把本次学到的偏好合并进已有偏好，保留人工设置的内容。"""
    out = deepcopy(base or {})
    for key, value in learned.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            for sub, sub_value in value.items():
                if isinstance(sub_value, list) and isinstance(out[key].get(sub), list):
                    merged = list(out[key][sub])
                    for item in sub_value:
                        if item not in merged:
                            merged.append(item)
                    out[key][sub] = merged
                else:
                    out[key][sub] = sub_value
        elif isinstance(value, list) and isinstance(out.get(key), list):
            merged = list(out[key])
            for item in value:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = value
    return out


def _trip_attraction_types(final_plan: dict) -> list[str]:
    """从一份已保存行程里提取所有景点的 category_label。"""
    types: list[str] = []
    for block in (final_plan or {}).get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if (block.get("type") or "") != "景点":
            continue
        label = str(block.get("category_label") or "").strip()
        if label:
            types.append(label)
    return types


def _liked_from_trips(
    user_id: str, db: Session, top_n: int = 3, min_rating: int = 4
) -> list[str]:
    """从最近 top_n 次高评价（rating>=min_rating）旅行里，返回去得最多的景点类型。"""
    trips = list(
        db.scalars(
            select(TripMemory)
            .where(TripMemory.user_id == user_id)
            .where(TripMemory.chosen_plan_style.is_not(None))
            .where(TripMemory.rating >= min_rating)
            .order_by(TripMemory.created_at.desc())
            .limit(top_n)
        )
    )
    counts: dict[str, int] = {}
    for trip in trips:
        for label in _trip_attraction_types(trip.final_plan):
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return []
    most = max(counts, key=lambda c: counts[c])
    return [most]


def aggregate_preferences(user_id: str, db: Session, min_count: int = 3) -> UserPreference:
    """读取该用户的行为信号，聚合偏好并 upsert 到 UserPreference。"""
    signals = list(
        db.scalars(
            select(BehaviorSignal)
            .where(BehaviorSignal.user_id == user_id)
            .order_by(BehaviorSignal.created_at.asc())
        )
    )
    current = db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    learned = _learn(signals, min_count)
    merged = _merge(current.preferences if current else {}, learned)
    # liked 由最近高评价行程确定性计算，覆盖规则/模型摘要的结果。
    merged["liked"] = _liked_from_trips(user_id, db)

    if current is None:
        current = UserPreference(user_id=user_id, preferences=merged)
        db.add(current)
    else:
        current.preferences = merged
    db.commit()
    db.refresh(current)
    return current


def _summarize_conversation(conversation: list) -> dict:
    """调用 PlanAgent venv 里的轻量模型，把用户对话提炼成偏好 JSON。"""
    if not conversation:
        return {}
    try:
        env = dict(os.environ)
        env.pop("__PYVENV_LAUNCHER__", None)
        proc = subprocess.run(
            [str(PLAN_PYTHON), str(CONVERSATION_SUMMARY_PY)],
            input=json.dumps({"conversation": conversation}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        data = json.loads(proc.stdout)
    except Exception:
        return {}
    prefs = data.get("preferences") if isinstance(data, dict) else None
    return prefs if isinstance(prefs, dict) else {}


def merge_conversation_preferences(
    user_id: str, conversation: list, db: Session
) -> UserPreference | None:
    """从对话中提取偏好并合并到 UserPreference，失败时返回 None。"""
    learned = _summarize_conversation(conversation)
    if not learned:
        return None
    current = db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    merged = _merge(current.preferences if current else {}, learned)
    if current is None:
        current = UserPreference(user_id=user_id, preferences=merged)
        db.add(current)
    else:
        current.preferences = merged
    db.commit()
    db.refresh(current)
    return current


def _summarize_plan(plan: dict) -> dict:
    """调用 PlanAgent venv 里的轻量模型，把已确认计划提炼成偏好 JSON。"""
    if not plan:
        return {}
    try:
        env = dict(os.environ)
        env.pop("__PYVENV_LAUNCHER__", None)
        proc = subprocess.run(
            [str(PLAN_PYTHON), str(PLAN_SUMMARY_PY)],
            input=json.dumps({"plan": plan}, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=env,
        )
        data = json.loads(proc.stdout)
    except Exception:
        return {}
    prefs = data.get("preferences") if isinstance(data, dict) else None
    return prefs if isinstance(prefs, dict) else {}


def merge_trip_preferences(
    user_id: str, final_plan: dict, conversation: list, db: Session
) -> UserPreference | None:
    """从「确认的计划 + 对话」中提炼偏好并合并进 UserPreference。"""
    learned: dict = {}
    learned = _merge(learned, _summarize_plan(final_plan))
    learned = _merge(learned, _summarize_conversation(conversation))
    # liked 由最近高评价行程确定性计算，不采纳模型摘要里的 liked。
    learned.pop("liked", None)
    current = db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    merged = _merge(current.preferences if current else {}, learned)
    merged["liked"] = _liked_from_trips(user_id, db)
    if current is None:
        current = UserPreference(user_id=user_id, preferences=merged)
        db.add(current)
    else:
        current.preferences = merged
    db.commit()
    db.refresh(current)
    return current


def _trip_hash(recent_trips: list[dict]) -> str:
    """与 Orchestrator 的 _trip_hash 保持一致，用于缓存命中判断。"""
    payload = json.dumps(recent_trips, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _summarize_recent_trips(recent_trips: list[dict]) -> str:
    """调用 PlanAgent venv 里的轻量模型，把最近行程摘要成稳定偏好。"""
    if not recent_trips:
        return ""
    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)
    # 轻量模型偶发返回空摘要，重试一次提升落库稳定性。
    for _ in range(2):
        try:
            proc = subprocess.run(
                [str(PLAN_PYTHON), str(TRIP_SUMMARY_PY)],
                input=json.dumps(
                    {"recent_trips": recent_trips}, ensure_ascii=False
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                env=env,
            )
            data = json.loads(proc.stdout)
        except Exception:
            continue
        summary = data.get("summary") if isinstance(data, dict) else None
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def save_trip_summary(
    user_id: str, recent_trips: list[dict], db: Session
) -> MemoryCache | None:
    """确认计划/评价后生成摘要并 upsert 到 MemoryCache，供下次规划复用。"""
    summary = _summarize_recent_trips(recent_trips)
    if not summary:
        return None
    cache = db.scalar(select(MemoryCache).where(MemoryCache.user_id == user_id))
    if cache is None:
        cache = MemoryCache(user_id=user_id)
        db.add(cache)
    cache.trip_summary = summary
    cache.trip_summary_hash = _trip_hash(recent_trips)
    db.commit()
    db.refresh(cache)
    return cache
