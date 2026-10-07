"""PlanAgent：读取 SearchAgent 的结构化输出 + 用户画像，生成逐日旅行计划。

用法：
  # 先跑 SearchAgent 拿到结构化结果，再喂给 PlanAgent
  echo '{"destination":"宁波","start_date":"2026-10-01","end_date":"2026-10-03"}' \
    | python ../SearchAgent/search.py \
    | python plan.py

输入（二选一）：
  1) SearchAgent 返回的 JSON（weather/hotels/poi/promotions），不带画像/偏好
  2) {"profile": {...用户画像...}, "search": {...搜索结果...}}
输出：旅行计划 JSON（含逐日 itinerary）
"""

import json
import math
import os
import re
import subprocess
import sys
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from route_map import (
    _km,
    attach_routes,
    driving_travel,
    geocode,
    geocode_blocks,
    strip_geo,
    walking_route,
)
from plan_state import merge_block_edits, rebuild_itineraries
from trip_changes import resolve_trip_changes

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
ROOT = BASE_DIR.parent
sys.path.insert(0, str(ROOT))
from shared.sources import merge_plan_sources
from shared.audit import without_review

SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = ROOT / "SearchAgent" / ".venv" / "bin" / "python"


def _model_options() -> dict:
    # 规划、补全、编辑无需长思考，避免 Qwen 默认思考模式拖慢交互。
    model = os.getenv("OPENAI_MODEL", "qwen3.8-27b").lower()
    if "qwen" in model or "deepseek" in model:
        return {"extra_body": {"enable_thinking": False}}
    return {}


def _chat_completion(client: OpenAI, **kwargs):
    return client.chat.completions.create(**{**_model_options(), **kwargs})

DAY_PLAN_PROMPT = (
    "你是旅行规划师，负责规划某一天的行程。"
    "输入包含 day（第几天）、date、block（当天分配的景点片区，names 是可选的景点名）、"
    "search（酒店/机票/高铁等）、user_profile、preferences、basic 等。"
    "规则：根据 preferences.pace 调整每天景点数量（以 block.min_spots~max_spots 为准）："
    "慢节奏→2~3 个景点、安排休息；适中/休闲或无节奏→3~4 个；快节奏/紧凑→4~6 个，优先网红出片点、行程充实。"
    "根据预算档位安排：经济档→免费景点+公共交通+经济酒店/餐厅；舒适档→中等门票+地铁/打车+舒适酒店；"
    "豪华档→可付费体验+打车+高星酒店/高品质餐厅；总花费不超 basic.total_budget。"
    "根据画像与人数：带老人/孩子减少步行、安排休息；学生控预算；退休轻松节奏；美食偏好优先安排当地特色餐厅。"
    "如果是第一天且去程航班/高铁到达时间较晚（15:00 以后），可只安排 1 个景点或仅安排晚餐/入住；"
    "如果到达时间晚于 20:00，第一天不要再安排景点，只安排交通和入住；禁止在 00:00 之后安排景点。"
    "其他情况当天至少安排 block.min_spots 个景点；若 block.names 不够，可从 search.poi 里选地理相邻的景点补充。"
    "根据景点 scale/duration 安排数量与时间：大景点(全天)每天 1 个，中景点每天 2~3 个，小景点每天最多 6 个；"
    "不要在同一天塞入两个全天型大景点。"
    "优先选择榜单名次靠前（rank 小）且 match_score（匹配度，0-100）较高的景点；如果当天 block 内有全天型高热度景点，优先把它安排进去，"
    "其他可顺延的小景点放到别的天。"
    "根据当天天气安排室内外：search.weather 里当天若是下雨/下雪/阴雨等天气，优先安排 description 含「室内」的景点（博物馆、展馆、商场、剧场等），"
    "把户外景点放到晴天或缩短游览、改到天气转好的时段；晴天则优先户外景点。"
    "按时间与距离合理安排：当天所有景点的 duration 累加（含交通）控制在 8 小时以内；"
    "优先把同区县的景点排在一起，相邻景点交通耗时尽量不超过 30 分钟；"
    "跨区且交通超过 1 小时的景点不要硬塞在同一天。"
    "当天景点必须按地理顺序排成一条不折返的路线：如果 C 离 A 很近，就应安排 A→C→B，而不是 A→B→C。"
    "最后一天若回程时间在傍晚或晚上，不要安排距离车站/机场超过 1 小时的远郊景点。"
    "schedule 必须按时间从早到晚排列。"
    "若当天出现 2 小时以上未安排的空档（不含用餐/交通），优先从 block.names 补一个中/小景点；"
    "若没有合适景点，则安排 1 个 type=活动 的休闲/自由活动块填满空档。"
    "activity_window是当天可游玩的时间范围，所有景点必须完整位于这个范围；时间不足宁可减少景点。"
    "在可游玩的午餐11:00-14:00和晚餐17:00-20:00窗口，各预留45-60分钟完整就餐空档及接驳余量。"
    "半天/全天景点可安排部分区域或缩短游览，不能用连续6小时景点占满整个午餐窗口；不要为了多排景点取消用餐。"
    "当天只从 block.names 里选景点；"
    "本阶段不要安排美食/餐饮，餐饮稍后会单独补充。"
    "本阶段不要写交通项，去程和回程会按已核实班次确定性补入；禁止自行选择或编造其他交通时间。"
    "本阶段不要写酒店，hotel 字段留空字符串；酒店会在所有行程排定后，按全部景点的相对中心统一选择。"
    "输出 JSON：{\"day\":N,\"date\":\"YYYY-MM-DD\",\"theme\":\"当天主题\",\"hotel\":\"酒店名\","
    "\"schedule\":[{\"time\":\"09:00-11:00\",\"type\":\"景点\",\"name\":\"活动名\",\"note\":\"简短说明\"}]}。"
    "只输出 JSON，不要任何多余文字或代码块。"
)


CLASSIFY_MODIFY_PROMPT = (
    "你是旅行计划修改意图分类器。根据用户指令和现有行程块，判断修改方式。"
    "如果指令指向某个具体景点/酒店/活动（例如「我不想去雷峰塔」「把酒店换成豪华型」），"
    "mode=block，并在 targets 中列出涉及的块（每项含 name/type/day）；"
    "如果是整体意见（例如「行程太紧」「预算太高」「节奏慢一点」），mode=global，targets=[]。"
    "输出 JSON：{\"mode\":\"global或block\",\"targets\":[{\"name\":\"...\",\"type\":\"...\",\"day\":1}]}。"
    "只输出 JSON，不要任何多余文字。"
)


MODIFY_PROMPT = (
    "你是旅行计划修改助手。blocks 是明确选中的待修改项目，full_blocks 是相邻行程供参考。"
    "只修改 blocks 中的项目，保留它们的 id、day、date、plan_style 和所有未涉及字段，禁止修改其他项目。"
    "根据 instruction 调整项目、时间、节奏、价格或备注；替换地点优先使用 search 里真实候选。"
    "新地点不得沿用旧地点的经纬度、链接、评分或餐厅选项，查不到的新字段请省略。"
    "考虑前后景点位置和已有时间，单日时间安排在06:00-23:00，避免冲突和远距离折返。"
    "餐厅应靠近相邻景点，不要只是追求高评分。无法合理替换时保留原项目并在note解释原因。"
    "输出 JSON：{\"blocks\":[修改后的选中 block...]}，顺序与输入一致。"
    "只输出 JSON，不要任何多余文字或代码块。"
)

MODIFY_PLAN_PROMPT = (
    "你是旅行计划修改助手。根据用户指令修改完整的旅行计划。"
    "输入 plan（完整计划，含 destination/start_date/end_date/days/weather_summary/plans，"
    "每个 plan 含 style/summary/itinerary，itinerary 每天含 schedule）和 modify（修改指令）。"
    "modify 含 mode 和 instruction："
    "1) mode='global'：全局修改。按 instruction 整体调整计划（例如「行程太紧了」→减少每天景点数量、"
    "放慢节奏、增加休息或自由活动时间；「预算太高」→换成更便宜的选择）；"
    "2) mode='block'：block 修改。只修改或删除 modify.targets 指定的那些活动块"
    "（targets 是列表，每项含 name/type/day，例如「我不想去这里」→删除 target 对应的块，"
    "并合理顺延或填补后续安排），其余块尽量保持不变。"
    "输出修改后的完整 plan JSON，保持原结构（destination/start_date/end_date/days/weather_summary/plans/...）。"
    "每个 schedule 项含 time/type/name/note/link，type 取值 交通/美食/景点/酒店/活动。"
    "只输出 JSON，不要任何多余文字或代码块。"
)

BLOCK_MODIFY_PROMPT = (
    "你是旅行计划修改助手。根据 instruction 修改某几天的行程。"
    "输入 days（需要修改的几天 itinerary，每天含 day/date/theme/hotel/schedule）和 modify"
    "（targets=要操作的块列表，每项含 name/type/day；instruction=指令）。"
    "只修改或删除 targets 对应的块，必要时顺延当天后续块的时间或填补空档；未涉及的块和天保持不变。"
    "输出 JSON：{\"days\":[修改后的 day itinerary...]}，每天结构保持 day/date/theme/hotel/schedule。"
    "schedule 项含 time/type/name/note/link。只输出 JSON，不要任何多余文字或代码块。"
)

def _build_meta(search_result: dict) -> dict:
    """从搜索结果本地提取行程元数据，省掉 LLM 生成外层字段。"""
    destination = search_result.get("destination") or ""
    start_date = search_result.get("start_date") or ""
    end_date = search_result.get("end_date") or ""
    days = 0
    if start_date and end_date:
        try:
            days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
        except ValueError:
            days = 0
    weather = search_result.get("weather") or {}
    weather_days = weather.get("days") or []
    weather_summary = ""
    if weather_days:
        first = weather_days[0]
        weather_summary = (
            f"{weather.get('location', destination)} {start_date} 至 {end_date}，"
            f"首日{first.get('weather', '')}，共 {len(weather_days)} 天"
        )
    return {
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "weather_summary": weather_summary,
    }


def _trim_search(search: dict) -> dict:
    """精简 search 结果：去掉长文本与无关字段，降低 PlanAgent 的 prompt 长度。"""
    result: dict = {}
    for key in ("destination", "start_date", "end_date"):
        if search.get(key):
            result[key] = search[key]

    weather = search.get("weather")
    if isinstance(weather, dict):
        result["weather"] = {
            "location": weather.get("location"),
            "days": [
                {k: d.get(k) for k in ("date", "weather", "temp_min", "temp_max", "humidity")}
                for d in (weather.get("days") or [])
            ],
        }

    if isinstance(search.get("poi"), list):
        result["poi"] = [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "rank": p.get("rank"),
                "match_score": p.get("match_score"),
                "free": p.get("free"),
                "district": p.get("district_label") or p.get("district") or "",
                "duration": p.get("duration") or "",
                "scale": p.get("scale") or "",
                "longitude": p.get("longitude"),
                "latitude": p.get("latitude"),
                "description": (p.get("description") or "")[:80],
            }
            for p in search["poi"]
        ]

    if isinstance(search.get("hotels"), list):
        result["hotels"] = [
            {
                "name": h.get("name"),
                "star": h.get("star"),
                "price": h.get("price"),
                "location": h.get("location"),
                "longitude": h.get("longitude"),
                "latitude": h.get("latitude"),
            }
            for h in search["hotels"]
        ]

    # food：第二阶段补入计划，仍保留结构化餐厅数据
    if isinstance(search.get("food"), list):
        result["food"] = [
            {
                "name": x.get("name"),
                "cuisine": x.get("cuisine"),
                "rating": x.get("rating"),
                "price_per_person": x.get("price_per_person"),
                "business_area": x.get("business_area"),
                "address": x.get("address"),
                "longitude": x.get("longitude"),
                "latitude": x.get("latitude"),
            }
            for x in search["food"]
            if x.get("name")
        ]

    for key in ("flights", "trains"):
        if isinstance(search.get(key), list):
            outbound = []
            inbound = []
            for x in search[key]:
                if key == "flights":
                    name = f"{x.get('airline') or ''}{x.get('flight_no') or ''}"
                else:
                    name = f"{x.get('transport') or ''}{x.get('train_no') or ''}"
                item = {
                    "name": name,
                    "from": x.get("dep_station") or "",
                    "to": x.get("arr_station") or "",
                    "time": x.get("dep_time") or "",
                    "price": x.get("price") or "",
                }
                if x.get("direction") == "回":
                    inbound.append(item)
                else:
                    outbound.append(item)
            result[key] = {"outbound": outbound[:3], "inbound": inbound[:3]}

    return result


def _chunks(items: list[str], count: int) -> list[list[str]]:
    count = max(1, min(count, len(items)))
    buckets: list[list[str]] = [[] for _ in range(count)]
    for index, item in enumerate(items):
        buckets[index % count].append(item)
    return [bucket for bucket in buckets if bucket]


def _short_district(value: str | None) -> str:
    """从完整地址里提取区县级行政区，用于分 block 时兜底。"""
    value = (value or "").strip()
    if not value:
        return ""
    candidates: list[tuple[int, str]] = []
    for suffix in ("自治县", "区", "县", "旗"):
        start = 0
        while True:
            idx = value.find(suffix, start)
            if idx == -1:
                break
            if suffix == "区" and idx >= 2 and value[idx - 2:idx] == "自治":
                start = idx + 1
                continue
            candidates.append((idx, suffix))
            break
    if not candidates:
        return ""
    pos, suffix = min(candidates, key=lambda item: item[0])
    prefix = value[:pos]
    cut = -1
    for sep in ("自治州", "自治区", "市", "省"):
        idx = prefix.rfind(sep)
        if idx != -1:
            cut = max(cut, idx + len(sep))
    name = prefix[cut:] if cut != -1 else prefix
    return f"{name}{suffix}"


def _matches_poi_name(name: str, candidate: str) -> bool:
    """景区/馆区可以有名称后缀，但“西湖天地”不能冒充“西湖”。"""
    def normalize(value: str) -> str:
        value = re.sub(r"[（(].*?[）)]", "", str(value)).strip()
        return re.sub(r"(?:风景名胜区|风景区|景区)$", "", value)
    expected, actual = normalize(name), normalize(candidate)
    return bool(expected and (expected == actual or (
        len(expected) >= 4 and actual.startswith(expected)
        and re.search(r"(?:馆区|分馆)$", actual)
    )))


def _rank_score(rank: str | None) -> int:
    """从飞猪榜单文案里解析名次，用于 block 内热度排序。"""
    import re as _re

    text = str(rank or "")
    match = _re.search(r"第\s*(\d+)\s*名", text)
    return int(match.group(1)) if match else 0


_POI_MATCH_KEYWORDS = {
    "自然景观": ["自然", "山水", "森林", "田园", "生态", "风光", "湖", "公园", "园林"],
    "历史人文": ["博物馆", "历史", "古迹", "纪念馆", "故居", "寺", "祠", "艺术", "人文", "古镇"],
    "主题娱乐": ["乐园", "动物园", "海洋", "主题", "娱乐", "温泉", "科普", "儿童"],
    "城市地标与购物": ["地标", "商圈", "购物", "商业", "市集", "文创"],
    "户外运动与体验": ["户外", "徒步", "露营", "探险", "漂流", "攀岩", "滑雪", "冲浪", "潜水"],
    "休闲度假": ["园林", "湖", "公园", "温泉", "古镇", "度假", "水乡"],
    "深度文化": ["博物馆", "历史", "古迹", "纪念馆", "故居", "寺", "祠", "艺术", "人文"],
    "自然风光": ["自然", "山水", "森林", "田园", "生态", "风光", "湖", "公园"],
    "美食探店": ["美食", "小吃", "市集", "夜市", "老字号", "餐饮"],
    "亲子乐园": ["亲子", "乐园", "动物园", "海洋", "科普", "儿童", "博物馆"],
    "购物血拼": ["购物", "商圈", "商业", "市集"],
    "冒险户外": ["户外", "徒步", "露营", "探险", "漂流", "攀岩"],
    "摄影旅拍": ["打卡", "网红", "拍照", "出片", "地标", "夜景", "风光"],
    "美食": ["美食", "小吃", "夜市", "老字号", "餐饮"],
    "亲子": ["亲子", "乐园", "动物园", "海洋", "科普", "儿童"],
    "文化": ["博物馆", "历史", "古迹", "纪念馆", "故居", "寺", "人文", "艺术"],
    "自然": ["自然", "山水", "森林", "田园", "湖", "公园", "风光"],
    "自然风景": ["自然", "山水", "森林", "田园", "湖", "公园", "风光"],
    "购物": ["购物", "商圈", "商业", "市集"],
    "摄影": ["打卡", "网红", "拍照", "出片", "地标", "夜景"],
    "冒险": ["户外", "徒步", "露营", "探险", "漂流"],
    "休闲": ["园林", "湖", "公园", "温泉", "古镇", "度假"],
}


def _dedupe_keywords(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _user_match_signals(profile, preferences, basic) -> tuple[list[str], list[str]]:
    """从画像/偏好/本次旅行信息中提取正、负向关键词，用于景点匹配度打分。"""
    positive: list[str] = []
    for source in (
        (profile or {}).get("travel_style"),
        (basic or {}).get("travel_style"),
        (basic or {}).get("purposes"),
    ):
        items = source or []
        if isinstance(items, str):
            items = [items]
        for item in items:
            item = str(item).strip()
            if not item:
                continue
            positive.append(item)
            positive.extend(_POI_MATCH_KEYWORDS.get(item, []))

    liked = (preferences or {}).get("liked") or []
    if isinstance(liked, str):
        liked = [liked]
    positive.extend(str(x).strip() for x in liked)

    avoided = (preferences or {}).get("avoided") or []
    if isinstance(avoided, str):
        avoided = [avoided]
    negative = [str(x).strip() for x in avoided]

    return _dedupe_keywords(positive), _dedupe_keywords(negative)


def _score_poi_match(poi: dict, profile, preferences, basic) -> int:
    """给单个景点打分（0-100），反映其与画像/偏好/本次旅行信息的匹配度。"""
    positive, negative = _user_match_signals(profile, preferences, basic)
    if not positive and not negative:
        return 50
    text = " ".join(
        str(poi.get(key) or "")
        for key in ("name", "category", "description", "scale", "duration", "district")
    )
    pos_hits = sum(1 for kw in positive if kw in text)
    neg_hits = sum(1 for kw in negative if kw in text)
    score = 50 + 12 * pos_hits - 18 * neg_hits
    return max(0, min(100, score))


def _filter_far_pois(search: dict, destination: str, max_km: float = 60.0) -> dict:
    """过滤掉离城市核心过远的景点（如千岛湖之于杭州主城）。"""
    pois = list(search.get("poi") or [])
    coords = []
    for p in pois:
        lng, lat = p.get("longitude"), p.get("latitude")
        if lng is not None and lat is not None:
            coords.append(f"{lng},{lat}")
    if not coords:
        return search
    lngs = sorted(float(c.split(",")[0]) for c in coords)
    lats = sorted(float(c.split(",")[1]) for c in coords)
    med = f"{lngs[len(lngs) // 2]},{lats[len(lats) // 2]}"
    core = [c for c in coords if _km(med, c) <= max_km / 2]
    if core:
        c_lngs = [float(c.split(",")[0]) for c in core]
        c_lats = [float(c.split(",")[1]) for c in core]
        center = f"{sum(c_lngs) / len(c_lngs)},{sum(c_lats) / len(c_lats)}"
    else:
        center = med
    kept = [
        p for p in pois
        if p.get("longitude") is None or p.get("latitude") is None
        or _km(center, f"{p['longitude']},{p['latitude']}") <= max_km
    ]
    if kept and len(kept) < len(pois):
        search = dict(search)
        search["poi"] = kept
    return search


def _assign_days_by_score(
    search: dict,
    days: int,
    min_spots: int,
    max_spots: int,
    travel_categories: list[str] | None = None,
    poi_map: dict[str, list[float]] | None = None,
    radius_km: float = 10.0,
) -> tuple[list[dict], dict[str, int]]:
    """热门度前 max(天数-1, 2) + 匹配风格 + 其余，按匹配度排序并按 10km 聚类后分配到每天。"""
    pois = [p for p in (search.get("poi") or []) if p.get("name")]
    if not pois:
        return [
            {"day": day, "names": [], "min_spots": min_spots, "max_spots": max_spots}
            for day in range(1, days + 1)
        ], {}
    travel_categories = set(travel_categories or [])

    def hot_key(p: dict) -> tuple[bool, int]:
        n = _rank_score(p.get("rank"))
        return (n == 0, n or 10**9)

    def rest_key(p: dict) -> tuple[int, int]:
        # 匹配度为主；匹配度相近时优先榜单景点
        match = int(p.get("match_score") or 0)
        ranked = int(_rank_score(p.get("rank")) > 0)
        return (-match, -ranked)

    def is_match(p: dict) -> bool:
        return bool(travel_categories and p.get("category_label") in travel_categories)

    top_n = sorted(pois, key=hot_key)[:max(2, days - 1)]
    matching = sorted(
        [p for p in pois if is_match(p)],
        key=lambda p: -int(p.get("match_score") or 0),
    )
    rest = sorted([p for p in pois if not is_match(p)], key=rest_key)

    ordered: list[dict] = []
    seen: set[str] = set()

    def add(p: dict) -> None:
        name = p.get("name")
        if name and name not in seen:
            seen.add(name)
            ordered.append(p)

    # 1. 热门度前 max(天数-1, 2)
    for p in top_n:
        add(p)
    # 2. 每天至少 1 个匹配风格的景点（不足则尽可能多；不满足路程会在后续舍弃）
    min_matching = days
    matched = sum(1 for p in ordered if is_match(p))
    for p in matching:
        if matched >= min_matching:
            break
        if p.get("name") not in seen:
            add(p)
            matched += 1
    # 3. 其余
    for p in rest:
        add(p)
    # 4. 剩余匹配风格
    for p in matching:
        add(p)

    # 按 10km 聚类后，把同 block 的景点排在一起（块内保持原匹配度顺序），
    # 这样每天内部的景点地理相邻，且同 block 的天天然相邻。
    name_to_block: dict[str, int] = {}
    if poi_map:
        name_to_block = _cluster_by_radius(
            [p.get("name") or "" for p in ordered], poi_map, radius_km
        )
        blocks: dict[int, list[dict]] = {}
        block_order: list[int] = []
        for p in ordered:
            b = name_to_block.get(p.get("name") or "", -1)
            if b not in blocks:
                blocks[b] = []
                block_order.append(b)
            blocks[b].append(p)
        ordered = [p for b in block_order for p in blocks[b]]

    assignments = []
    for day in range(1, days + 1):
        chunk = ordered[(day - 1) * max_spots : day * max_spots]
        assignments.append({
            "day": day,
            "names": [p["name"] for p in chunk],
            "min_spots": min_spots,
            "max_spots": max_spots,
        })
    return assignments, name_to_block


def _pace_tier(pace: str, styles: list[str]) -> str:
    """返回节奏档位：slow / moderate / fast。"""
    slow_words = ("慢",)
    fast_words = ("快", "紧凑", "赶", "暴走", "特种兵")
    if any(word in pace for word in slow_words):
        return "slow"
    if any(word in pace for word in fast_words):
        return "fast"
    return "moderate"


def _pace_spots(pace: str, styles: list[str]) -> tuple[int, int]:
    """按节奏返回每天景点数量区间（最小, 最大）。"""
    return {"slow": (2, 3), "moderate": (3, 4), "fast": (4, 6)}[_pace_tier(pace, styles)]


def _pace_block_limit(pace: str, styles: list[str]) -> int:
    """按节奏返回单个 block 的景点数上限，超过则拆成两天。"""
    return {"slow": 5, "moderate": 7, "fast": 10}[_pace_tier(pace, styles)]


def _build_blocks(
    search: dict,
    destination: str,
    cluster_km: float = 2.5,
    max_km: float = 60.0,
) -> list[dict]:
    """按区县标签 + 高德坐标聚类，把景点分成一个个地理 block。"""
    pois = search.get("poi") or []
    items: list[dict] = []
    for p in pois:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        lng = p.get("longitude")
        lat = p.get("latitude")
        if lng is not None and lat is not None:
            loc = f"{lng},{lat}"
        else:
            hit = geocode(name, destination)
            loc = (hit or {}).get("location") or ""
        district = (
            p.get("district_label")
            or _short_district(p.get("district") or "")
            or ""
        )
        items.append(
            {
                "name": name,
                "district": district,
                "loc": loc,
                "score": _rank_score(p.get("rank") or ""),
            }
        )

    # 以城市中心为基准，先过滤掉过远的景点（如千岛湖之于杭州主城）。
    # 城市中心取「离中位数 max_km/2 以内的城市核心」质心，对远郊离群点稳健；
    # 拿不到坐标时回退地理编码。
    coords = [item["loc"] for item in items if item["loc"]]
    if coords:
        lngs = sorted(float(c.split(",")[0]) for c in coords)
        lats = sorted(float(c.split(",")[1]) for c in coords)
        med = f"{lngs[len(lngs) // 2]},{lats[len(lats) // 2]}"
        core = [c for c in coords if _km(med, c) <= max_km / 2]
        if core:
            c_lngs = [float(c.split(",")[0]) for c in core]
            c_lats = [float(c.split(",")[1]) for c in core]
            city_loc = f"{sum(c_lngs) / len(c_lngs)},{sum(c_lats) / len(c_lats)}"
        else:
            city_loc = med
    else:
        city_hit = (
            geocode(destination, destination)
            or geocode(f"{destination}市", destination)
            or geocode(destination, "")
        )
        city_loc = (city_hit or {}).get("location") or ""
    if city_loc:
        kept: list[dict] = []
        for item in items:
            if not item["loc"]:
                kept.append(item)
                continue
            if _km(city_loc, item["loc"]) <= max_km:
                kept.append(item)
        if kept:
            items = kept

    by_district: dict[str, list[dict]] = {}
    for item in items:
        key = item["district"] or "__none__"
        by_district.setdefault(key, []).append(item)

    blocks: list[dict] = []
    for district, members in by_district.items():
        if district == "__none__":
            continue
        members.sort(key=lambda m: (m.get("score", 0) == 0, m.get("score", 0)))
        names = [m["name"] for m in members]
        scores = [m.get("score", 0) for m in members]
        locs = [m["loc"] for m in members if m["loc"]]
        center = None
        if locs:
            lngs = [float(loc.split(",")[0]) for loc in locs]
            lats = [float(loc.split(",")[1]) for loc in locs]
            center = [sum(lngs) / len(lngs), sum(lats) / len(lats)]
        block_id = f"{district}-1"
        blocks.append(
            {
                "block_id": block_id,
                "district": "" if district == "__none__" else district,
                "names": names,
                "scores": scores,
                "center": center,
            }
        )

    # 没有标准区县标签的景点，就近并到已有 block
    for item in by_district.get("__none__", []):
        if not blocks:
            blocks.append(
                {
                    "block_id": "block-1",
                    "district": "",
                    "names": [item["name"]],
                    "scores": [item.get("score", 0)],
                    "center": None,
                }
            )
            continue
        candidates = [i for i, b in enumerate(blocks) if b.get("center")]
        if candidates and item["loc"]:
            target = min(
                candidates,
                key=lambda i: _km(
                    f"{blocks[i]['center'][0]},{blocks[i]['center'][1]}",
                    item["loc"],
                ),
            )
        else:
            target = max(
                range(len(blocks)),
                key=lambda i: len(blocks[i].get("names") or []),
            )
        blocks[target]["names"].append(item["name"])
        blocks[target]["scores"].append(item.get("score", 0))
        if blocks[target].get("scores"):
            pairs = sorted(
                zip(blocks[target]["scores"], blocks[target]["names"]),
                key=lambda pair: -pair[0],
            )
            blocks[target]["scores"] = [s for s, _ in pairs]
            blocks[target]["names"] = [n for _, n in pairs]
        if item["loc"] and not blocks[target].get("center"):
            lng, lat = item["loc"].split(",")
            blocks[target]["center"] = [float(lng), float(lat)]

    return blocks


def _merge_small_blocks(blocks: list[dict], min_size: int = 3) -> list[dict]:
    """把小 block 合并到最近的 block，避免出现大量单点 block。"""
    working = [dict(b) for b in blocks]
    while True:
        small = [i for i, b in enumerate(working) if len(b.get("names") or []) < min_size]
        if not small:
            break
        index = small[0]
        block = working[index]
        candidates = [j for j, b in enumerate(working) if j != index and b.get("center")]
        if candidates:
            target = min(
                candidates,
                key=lambda j: _km(
                    f"{working[j]['center'][0]},{working[j]['center'][1]}",
                    f"{block['center'][0]},{block['center'][1]}",
                )
                if block.get("center")
                else 0,
            )
            if block.get("center") is None:
                target = max(candidates, key=lambda j: len(working[j].get("names") or []))
        else:
            if len(working) <= 1:
                break
            target = max(
                [j for j in range(len(working)) if j != index],
                key=lambda j: len(working[j].get("names") or []),
            )
        working[target]["names"] = (working[target].get("names") or []) + (block.get("names") or [])
        working[target]["scores"] = (working[target].get("scores") or []) + (block.get("scores") or [])
        if working[target].get("scores"):
            pairs = sorted(
                zip(working[target]["scores"], working[target]["names"]),
                key=lambda pair: -pair[0],
            )
            working[target]["scores"] = [s for s, _ in pairs]
            working[target]["names"] = [n for _, n in pairs]
        working[target]["district"] = working[target].get("district") or block.get("district") or ""
        working.pop(index)
    return working


def _split_block(block: dict, count: int) -> list[dict]:
    parts = _chunks(block.get("names") or [], count)
    score_parts = _chunks(block.get("scores") or [], count)
    return [
        {
            "block_id": f"{block['block_id']}-{i + 1}",
            "district": block.get("district") or "",
            "names": part,
            "scores": score_parts[i] if i < len(score_parts) else [],
            "center": block.get("center"),
        }
        for i, part in enumerate(parts)
    ]


def _transport_datetime(value: str, day: str) -> datetime | None:
    value = str(value or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", value):
        value = f"{day}T{value}"
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _select_transport(search_result: dict, basic: dict | None = None) -> dict:
    """从完整搜索结果选择真实班次；短途优先早去晚回的列车。"""
    if "outbound" in search_result and "inbound" in search_result:
        return search_result
    result = {"outbound": None, "inbound": None}
    for direction, key, day in (
        ("去", "outbound", search_result.get("start_date") or ""),
        ("回", "inbound", search_result.get("end_date") or search_result.get("start_date") or ""),
    ):
        candidates = []
        for source, kind in (("trains", "train"), ("flights", "flight")):
            items = search_result.get(source)
            if not isinstance(items, list):
                continue
            for item in items:
                if item.get("direction") != direction:
                    continue
                dep = _transport_datetime(item.get("dep_time"), day)
                arr = _transport_datetime(item.get("arr_time"), day)
                if dep is None or arr is None or dep.date().isoformat() != day:
                    continue
                if arr < dep and not re.search(r"\d{4}-\d{2}-\d{2}", str(item.get("arr_time") or "")):
                    arr += timedelta(days=1)
                duration = (arr - dep).total_seconds() / 60
                if duration <= 0 or duration > 24 * 60:
                    continue
                candidates.append({**item, "kind": kind, "dep_dt": dep.isoformat(),
                                   "arr_dt": arr.isoformat(), "duration_minutes": duration})
        short_trains = [item for item in candidates if item["kind"] == "train" and item["duration_minutes"] <= 240]
        if short_trains:
            candidates = short_trains
        def score(item: dict) -> float:
            dep = datetime.fromisoformat(item["dep_dt"])
            arr = datetime.fromisoformat(item["arr_dt"])
            dep_min, arr_min = dep.hour * 60 + dep.minute, arr.hour * 60 + arr.minute
            flight_overhead = 180 if item["kind"] == "flight" else 0
            if direction == "去":
                return ((arr.date() - dep.date()).days * 1440 + arr_min
                        + max(0, 7 * 60 - dep_min) * 3 + flight_overhead)
            return (abs(dep_min - 20 * 60) + max(0, 17 * 60 - dep_min) * 5
                    + max(0, arr_min - 23 * 60) * 3
                    + (arr.date() - dep.date()).days * 1440 + flight_overhead)
        if candidates:
            result[key] = min(candidates, key=score)
    return result


def _activity_window(date_: str, transport: dict) -> tuple[int, int]:
    """到站/机场之后留市内接驳时间，返程之前留进站/机场时间。"""
    try:
        midnight = datetime.fromisoformat(date_)
    except (TypeError, ValueError):
        return 8 * 60 + 30, 20 * 60
    start, end = 8 * 60 + 30, 20 * 60
    outbound, inbound = transport.get("outbound"), transport.get("inbound")
    if outbound:
        arrival = _transport_datetime(outbound.get("arr_dt"), date_)
        if arrival:
            buffer = 120 if outbound.get("kind") == "flight" else 60
            start = max(start, int((arrival + timedelta(minutes=buffer) - midnight).total_seconds() / 60))
    if inbound:
        departure = _transport_datetime(inbound.get("dep_dt"), date_)
        if departure:
            buffer = 120 if inbound.get("kind") == "flight" else 60
            end = min(end, int((departure - timedelta(minutes=buffer) - midnight).total_seconds() / 60))
    return start, end


def _first_day_arrives_late(search_result: dict) -> bool:
    """只判断选中的去程，不能把列表中任何一个晚班当作用户的去程。"""
    selected = _select_transport(search_result)
    outbound = selected.get("outbound")
    if not outbound:
        return False
    arrival = datetime.fromisoformat(outbound["arr_dt"])
    departure = datetime.fromisoformat(outbound["dep_dt"])
    return arrival.date() > departure.date() or arrival.hour >= 15


def _score_blocks(
    blocks: list[dict],
    city_center: list[float] | None = None,
    match_map: dict[str, int] | None = None,
) -> list[dict]:
    """给每个 block 打分：0.4 匹配度 + 0.2 数量 + 0.4 排名，降序返回。"""
    match_map = match_map or {}
    scored: list[tuple[float, dict]] = []
    for block in blocks:
        names = block.get("names") or []
        count = len(names)

        # 匹配度：block 内各景点 match_score 的平均（0-100 → 0-1）
        matches = [match_map.get(name, 0) for name in names]
        match = (sum(matches) / len(matches)) / 100.0 if matches else 0.0

        # 数量：景点越多越好，8 个封顶
        count_score = min(1.0, count / 8.0)

        # 排名：rank 越小越靠前，用 1/rank 的平均值
        scores = [s for s in (block.get("scores") or []) if s and s > 0]
        rank = sum(1.0 / s for s in scores) / len(scores) if scores else 0.0

        total = 0.4 * match + 0.2 * count_score + 0.4 * rank
        scored.append((total, block))

    scored.sort(key=lambda pair: -pair[0])
    return [block for _, block in scored]


def _drop_lowest_match(block: dict, match_map: dict[str, int]) -> dict:
    """丢弃 block 内匹配度最低的景点，保持 names/scores 对齐。"""
    names = list(block.get("names") or [])
    scores = list(block.get("scores") or [])
    if not names:
        return block
    index = min(range(len(names)), key=lambda i: match_map.get(names[i], 0))
    names.pop(index)
    if index < len(scores):
        scores.pop(index)
    block = dict(block)
    block["names"] = names
    block["scores"] = scores
    return block


def _order_units_geographically(units: list[dict], city_center: list[float] | None) -> list[dict]:
    """把选出的 block 按地理就近串成一天接一天，最后一天尽量靠近市中心。"""
    if len(units) <= 1 or not all(u.get("center") for u in units):
        return units

    located = [u for u in units if u.get("center")]
    if city_center:
        global_center = city_center
    else:
        total_w = sum(len(u.get("names") or []) for u in located) or 1
        global_center = [
            sum((u["center"][0] * len(u.get("names") or [])) for u in located) / total_w,
            sum((u["center"][1] * len(u.get("names") or [])) for u in located) / total_w,
        ]

    ordered = [units[0]]
    remaining = units[1:]
    while remaining:
        last = ordered[-1]
        nxt = min(
            remaining,
            key=lambda u: _km(
                f"{last['center'][0]},{last['center'][1]}",
                f"{u['center'][0]},{u['center'][1]}",
            ),
        )
        ordered.append(nxt)
        remaining.remove(nxt)

    if len(ordered) >= 3 and global_center:
        def distance_to_center(u: dict) -> float:
            return _km(
                f"{u['center'][0]},{u['center'][1]}",
                f"{global_center[0]},{global_center[1]}",
            )

        first = ordered[0]
        rest = ordered[1:]
        last_day = min(rest, key=distance_to_center)
        middle = [u for u in rest if u is not last_day]
        ordered = [first, *middle, last_day]
    return ordered


def _assign_blocks_to_days(
    blocks: list[dict],
    days: int,
    first_day_late: bool = False,
    city_center: list[float] | None = None,
    match_map: dict[str, int] | None = None,
    block_limit: int | None = None,
) -> list[dict]:
    """按 block 分数挑选，超大 block 拆成两天并丢弃最低分景点。"""
    if not blocks or days <= 0:
        return []
    match_map = match_map or {}

    # 丢弃景点数 ≤ 2 的 block；若因此清空则退回全部，避免无 block 可选。
    candidates = [dict(b) for b in blocks if len(b.get("names") or []) > 2]
    if not candidates:
        candidates = [dict(b) for b in blocks]

    scored = _score_blocks(candidates, city_center, match_map)

    units: list[dict] = []
    for block in scored:
        if len(units) >= days:
            break
        if block_limit and len(block.get("names") or []) > block_limit and len(units) + 2 <= days:
            block = _drop_lowest_match(block, match_map)
            units.extend(_split_block(block, 2))
        else:
            units.append(block)

    # block 不够填满天数时，拆最大的 block
    while len(units) < days:
        units.sort(key=lambda b: -len(b.get("names") or []))
        largest = units[0]
        if len(largest.get("names") or []) < 2:
            break
        units = units[1:] + _split_block(largest, 2)

    # 仍不够时补空 block
    while len(units) < days:
        units.append({"block_id": f"empty-{len(units) + 1}", "district": "", "names": [], "scores": [], "center": None})

    units = _order_units_geographically(units, city_center)

    assignments = []
    for i, unit in enumerate(units):
        assignments.append(
            {
                "day": i + 1,
                "block_id": unit.get("block_id"),
                "district": unit.get("district"),
                "names": unit.get("names") or [],
                "center": unit.get("center"),
            }
        )
    if first_day_late and len(assignments) > 1:
        assignments[0], assignments[1] = assignments[1], assignments[0]
    for index, assignment in enumerate(assignments):
        assignment["day"] = index + 1
    return assignments


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _schedule_range(item: dict) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s*", str(item.get("time") or ""))
    if not match:
        return None
    start, end = _to_minutes(match[1]), _to_minutes(match[2])
    return (start, end) if start is not None and end is not None and 0 <= start < end <= 1440 else None


def _visit_minutes(poi: dict, item: dict) -> int:
    duration = str(poi.get("duration") or "")
    if "全天" in duration:
        return 360
    if "半天" in duration:
        return 240
    hours = re.findall(r"\d+(?:\.\d+)?", duration)
    if hours and "小时" in duration:
        return max(30, min(360, int(float(hours[-1]) * 60)))
    interval = _schedule_range(item)
    return min(240, max(60, interval[1] - interval[0])) if interval else 120


def _transport_schedule_item(record: dict, direction: str) -> dict:
    departure, arrival = datetime.fromisoformat(record["dep_dt"]), datetime.fromisoformat(record["arr_dt"])
    number = record.get("train_no") or record.get("flight_no") or ""
    mode = record.get("transport") or record.get("airline") or ("列车" if record["kind"] == "train" else "航班")
    next_day = "（次日到达）" if arrival.date() > departure.date() else ""
    return {
        "type": "交通", "name": f"{direction}：{mode}{number} {record.get('dep_station') or ''}→{record.get('arr_station') or ''}",
        "time": f"{departure:%H:%M}-{arrival:%H:%M}",
        "note": f"已检索到的{direction}班次{next_day}；时间与余票以预订页为准",
        "link": record.get("url") or "", "price": record.get("price"),
        "transport_direction": direction, "departure_time": record["dep_dt"], "arrival_time": record["arr_dt"],
        "direction": "去" if direction == "去程" else "回", "dep_time": record["dep_dt"], "arr_time": record["arr_dt"],
    }


def _enforce_day_schedule(day_plan: dict, assignment: dict, date_: str,
                          transport: dict, search: dict) -> dict:
    """模型负责推荐内容，代码负责交通边界、时间顺序与重叠。"""
    day_plan = dict(day_plan)
    day_plan.update({"day": assignment.get("day"), "date": date_})
    start, end = _activity_window(date_, transport)
    day_plan["activity_window"] = {"start_min": start, "end_min": end,
                                    "start": _clock(min(1440, max(0, start))), "end": _clock(min(1440, max(0, end)))}
    pois = search.get("poi") if isinstance(search.get("poi"), list) else []
    poi_by_name = {poi.get("name"): poi for poi in pois}
    allowed = assignment.get("names") or []
    proposed = []
    seen = set()
    for item in day_plan.get("schedule") or []:
        if not isinstance(item, dict) or item.get("type") not in (None, "景点", "活动"):
            continue
        canonical = next((name for name in allowed if _matches_poi_name(name, item.get("name") or "")), None)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        proposed.append({**item, "type": "景点", "name": canonical})
    min_spots = int(assignment.get("min_spots") or 0)
    max_spots = int(assignment.get("max_spots") or 4)
    candidates = proposed[:max_spots]
    if min_spots and len(candidates) < min_spots:
        existing = {c["name"] for c in candidates}
        for name in allowed:
            if len(candidates) >= min_spots:
                break
            if name not in existing:
                candidates.append({"type": "景点", "name": name, "note": ""})
                existing.add(name)
    schedule = []
    # 抵达恰逢午餐时先留一小时，再从午餐附近的第一个景点开始游览。
    cursor = start + 60 if 11 * 60 <= start <= 13 * 60 else start
    previous_coord = None
    for item in candidates:
        poi = poi_by_name.get(item["name"], {})
        # 生成模型不能为地点补价格；只允许结构化来源票价或明确免费标记。
        for field in ("price", "unit_price", "price_known", "price_basis", "price_per_person", "ticketPrice", "ticket_price", "cost", "price_source"):
            item.pop(field, None)
        source_price = None
        for field in ("price", "price_per_person", "ticketPrice", "ticket_price"):
            value = poi.get(field)
            if isinstance(value, bool):
                continue
            try:
                text = str(value).strip().lstrip("¥￥").replace(",", "")
                number = float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else None
                if number is not None and math.isfinite(number) and number >= 0:
                    source_price = number
                    break
            except (TypeError, ValueError):
                continue
        if poi.get("free") is True:
            source_price = 0.0
        if source_price is not None:
            item.update(price=source_price, unit_price=source_price, price_known=True,
                        price_basis="per_person", price_source="search")
        coord = [poi.get("longitude"), poi.get("latitude")]
        if schedule:
            transfer = 30
            if previous_coord and _valid_food_coord(coord):
                leg = driving_travel(
                    f"{previous_coord[0]},{previous_coord[1]}",
                    f"{coord[0]},{coord[1]}",
                )
                if leg and (leg.get("duration_s") or 0) > 0:
                    transfer = max(30, math.ceil(leg["duration_s"] / 60))
                else:
                    km = _km(f"{previous_coord[0]},{previous_coord[1]}", f"{coord[0]},{coord[1]}")
                    transfer = max(30, math.ceil(km / 25 * 60) + 15)
                if transfer > 30:
                    continue
            if 11 * 60 <= cursor <= 13 * 60:
                cursor += 60
            cursor += transfer
        preferred = _schedule_range(item)
        earliest, finish_limit = cursor, end
        is_night_market = "夜市" in item["name"]
        if is_night_market:
            earliest = max(earliest, 17 * 60)
            opening = poi.get("opening_hours") or poi.get("business_hours")
            opening_span = _schedule_range({"time": opening}) if isinstance(opening, str) else None
            if opening_span:
                earliest, finish_limit = max(earliest, opening_span[0]), min(finish_limit, opening_span[1])
            item["note"] = (item.get("note") or "") + "；安排傍晚游览，请核实当天营业时间"
        proposed_start = max(earliest, preferred[0] if preferred and preferred[0] >= start else earliest)
        duration = _visit_minutes(poi, item)
        if proposed_start < 13 * 60 and proposed_start + duration > 13 * 60:
            # 保证13:00-14:00仍可用餐，全天景点只安排适当的半日区域。
            duration = 13 * 60 - proposed_start
            item["note"] = (item.get("note") or "") + "；保留午餐时间，仅安排部分区域游览"
        if proposed_start < 19 * 60 and proposed_start + duration > 19 * 60 and end >= 20 * 60:
            duration = 19 * 60 - proposed_start
            item["note"] = (item.get("note") or "") + "；保留晚餐时间"
        if proposed_start + duration > finish_limit and earliest + duration <= finish_limit:
            proposed_start = earliest
        if proposed_start + duration > finish_limit:
            if not is_night_market or finish_limit - proposed_start < 60:
                continue
            duration = finish_limit - proposed_start
            item["note"] = (item.get("note") or "") + "；受交通时间限制，缩短游览"
        if proposed_start < 0 or proposed_start + duration > 1440 or duration < 30:
            continue
        item["time"] = f"{_clock(proposed_start)}-{_clock(proposed_start + duration)}"
        if _valid_food_coord(coord):
            item.update({"lng": float(coord[0]), "lat": float(coord[1])})
            previous_coord = coord
        if poi.get("match_score") is not None:
            item["match_score"] = int(poi["match_score"])
        item["link"] = poi.get("url") or item.get("link") or ""
        schedule.append(item)
        cursor = proposed_start + duration
    for key, direction in (("outbound", "去程"), ("inbound", "返程")):
        record = transport.get(key)
        if record and str(record.get("dep_dt") or "")[:10] == date_:
            schedule.append(_transport_schedule_item(record, direction))
    schedule.sort(key=lambda item: _to_minutes(item.get("time") or "") if _to_minutes(item.get("time") or "") is not None else 1440)
    day_plan["schedule"] = schedule
    hotel_name = str(day_plan.get("hotel") or "").strip()
    hotels = search.get("hotels") if isinstance(search.get("hotels"), list) else []
    matched_hotel = next((hotel.get("name") for hotel in hotels if hotel.get("name") and hotel_name.startswith(hotel["name"])), None)
    day_plan["hotel"] = matched_hotel or ""
    if end <= start:
        day_plan["theme"] = "交通时间不足，当天不安排景点"
    inbound = transport.get("inbound")
    if inbound and str(inbound.get("dep_dt") or "")[:10] == date_:
        day_plan["hotel"] = ""
    return day_plan


def _locate_schedule(plans: list[dict], destination: str) -> dict[str, str | None]:
    """给所有 schedule 活动块做地理编码，返回 {name: "lng,lat"}（失败为 None）。"""
    names: list[str] = []
    for p in plans:
        for it in p.get("itinerary") or []:
            for s in it.get("schedule") or []:
                name = (s.get("name") or "").strip()
                if name and name not in names:
                    names.append(name)
    locations: dict[str, str | None] = {}
    for name in names:
        hit = geocode(name, destination)
        locations[name] = (hit or {}).get("location") or None
    return locations


def _reorder_by_proximity(plan: dict, destination: str, move_km: float = 2.0) -> dict:
    """片区已在生成前分配；保持日期，避免搬运后保留旧时间。

    跨日移动会突破到达/返程边界、制造重叠，并让两天景点堆到第一天。
    当天顺序由规划模型及确定性时间校正共同完成，餐厅再依最终顺序搜索。
    """
    return plan


def _dedupe_poi_names(plan: dict, search_result: dict) -> dict:
    """移除重复景点，不用不相关的全城候选替换原地点并沿用时间。"""
    for p in plan.get("plans") or []:
        seen: set[str] = set()
        for it in p.get("itinerary") or []:
            retained = []
            for s in it.get("schedule") or []:
                if s.get("type") != "景点":
                    retained.append(s)
                    continue
                name = (s.get("name") or "").strip()
                if not name:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                retained.append(s)
            it["schedule"] = retained
    return plan


def _backfill_links(plan: dict, search: dict) -> dict:
    """生成后按名称匹配回填链接，避免 url 进 prompt 导致 prompt 过长。"""
    entries: list[tuple[str, str]] = []
    for key in ("hotels", "poi", "food", "events", "promotions"):
        items = search.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            name = item.get("name") or item.get("title") or ""
            # 高德餐厅优先用 POI 详情链接，其次地图标记链接，最后通用 url
            url = (
                item.get("poi_detail_url")
                or item.get("map_url")
                or item.get("url")
                or ""
            )
            if name and url:
                entries.append((name, url))
    for key in ("flights", "trains"):
        items = search.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if key == "flights":
                name = f"{item.get('airline') or ''}{item.get('flight_no') or ''}"
            else:
                name = f"{item.get('transport') or ''}{item.get('train_no') or ''}"
            url = item.get("url") or ""
            if name and url:
                entries.append((name, url))

    def find_url(target: str) -> str:
        if not target:
            return ""
        for name, url in entries:
            if name == target:
                return url
        # 前缀/包含匹配：取最长的匹配名，避免「酒店名 + 房型后缀」匹配不上
        best = ""
        best_len = 0
        for name, url in entries:
            if target.startswith(name) or name.startswith(target):
                if len(name) > best_len:
                    best = url
                    best_len = len(name)
        return best

    for p in plan.get("plans") or []:
        for it in p.get("itinerary") or []:
            hotel = it.get("hotel") or ""
            it["hotel_link"] = find_url(hotel)
            for s in it.get("schedule") or []:
                name = s.get("name") or ""
                s["link"] = s.get("link") or find_url(name)
    return plan


def _build_day_plan(
    client: OpenAI,
    context: dict,
    day_assignment: dict,
    total_days: int,
    start_date: str,
) -> dict:
    """按单个地理 block 生成一天的行程。"""
    day = int(day_assignment.get("day") or 0)
    names = day_assignment.get("names") or []

    ctx = dict(context)
    search = dict(ctx.get("search") or {})
    if names and isinstance(search.get("poi"), list):
        name_set = set(names)
        search["poi"] = [p for p in search["poi"] if (p.get("name") or "") in name_set]
    ctx["search"] = search
    ctx["day"] = day
    ctx["block"] = {
        "block_id": day_assignment.get("block_id"),
        "district": day_assignment.get("district"),
        "names": names,
        "min_spots": day_assignment.get("min_spots"),
        "max_spots": day_assignment.get("max_spots"),
    }
    ctx["is_first_day"] = day == 1
    ctx["is_last_day"] = day == total_days
    try:
        ctx["date"] = (date.fromisoformat(start_date) + timedelta(days=day - 1)).isoformat()
    except ValueError:
        ctx["date"] = start_date
    activity_start, activity_end = _activity_window(ctx["date"], context.get("selected_transport") or {})
    ctx["activity_window"] = {"start": _clock(max(0, activity_start)), "end": _clock(max(0, activity_end))}
    if activity_end - activity_start < 60:
        return {"day": day, "date": ctx["date"], "theme": "交通时间不足，当天不安排景点", "hotel": "", "schedule": []}

    last: dict = {}
    for _ in range(2):
        try:
            resp = _chat_completion(client,
                model=os.getenv("OPENAI_MODEL", "qwen3.8-27b"),
                messages=[
                    {"role": "system", "content": DAY_PLAN_PROMPT},
                    {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                max_tokens=4000,
                timeout=60,
            )
            content = (resp.choices[0].message.content or "{}").strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
            last = json.loads(content)
        except Exception as exc:
            print(f"[plan] 第{day}天规划暂不可用：{type(exc).__name__}", file=sys.stderr)
            last = {}
        if isinstance(last, dict) and isinstance(last.get("schedule"), list):
            last["schedule"] = [item for item in last["schedule"] if isinstance(item, dict)]
            last["schedule"].sort(key=lambda s: (s.get("time") or "99:99"))
        if isinstance(last, dict) and last.get("schedule"):
            return last
    if not isinstance(last, dict):
        last = {}
    last.setdefault("day", day)
    last.setdefault("date", ctx.get("date") or "")
    last.setdefault("theme", "")
    last.setdefault("hotel", "")
    last.setdefault("schedule", [])
    if not last["schedule"] and names:
        target = max(2, int(day_assignment.get("min_spots") or 2))
        for index, name in enumerate(names[:target]):
            start = 9 * 60 + index * 150
            end = start + 120
            last["schedule"].append(
                {
                    "time": f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}",
                    "type": "景点",
                    "name": name,
                    "note": "",
                }
            )
    return last


def _pick_restaurant(
    food: list[dict],
    district: str,
    used: set[str],
    center: list[float] | None,
    budget_tier: str | None = None,
) -> dict | None:
    candidates = [
        f for f in food
        if (f.get("name") or "").strip() and (f.get("name") or "").strip() not in used
    ]
    if not candidates:
        return None
    if district:
        district_match = [
            f for f in candidates
            if district in str(f.get("business_area") or "")
            or district in str(f.get("address") or "")
        ]
        if district_match:
            candidates = district_match
    if budget_tier in ("豪华", "舒适", "经济"):
        def price_score(f: dict) -> float:
            price = f.get("price_per_person")
            return float(price) if price is not None else 0.0

        if budget_tier == "经济":
            candidates.sort(key=lambda f: (price_score(f), f.get("rating") is None, -(f.get("rating") or 0)))
        else:
            candidates.sort(key=lambda f: (-price_score(f), f.get("rating") is None, -(f.get("rating") or 0)))
    elif center:
        def distance(f: dict) -> float:
            lng = f.get("longitude")
            lat = f.get("latitude")
            if lng is None or lat is None:
                return float("inf")
            return _km(
                f"{center[0]},{center[1]}",
                f"{float(lng)},{float(lat)}",
            )

        candidates.sort(
            key=lambda f: (
                distance(f),
                f.get("rating") is None,
                -(f.get("rating") or 0),
            )
        )
    else:
        candidates.sort(key=lambda f: (f.get("rating") is None, -(f.get("rating") or 0)))
    return candidates[0]


def _pick_restaurants(
    food: list[dict],
    district: str,
    used: set[str],
    center: list[float] | None,
    budget_tier: str | None,
    limit: int = 3,
    meal_budget: float | None = None,
) -> list[dict]:
    """距离70%、评分20%、餐预算10%；可核实的步行绕路也进入距离惩罚。"""
    if not _valid_food_coord(center):
        return []
    target_price = meal_budget or {"经济": 60, "舒适": 120, "豪华": 250}.get(budget_tier or "")
    candidates: list[dict] = []
    seen: set[str] = set()
    for restaurant in food:
        if not isinstance(restaurant, dict):
            continue
        category = str(restaurant.get("cuisine") or "")
        if any(label in category for label in ("冷饮店", "甜品店", "糕饼店", "蛋糕店", "饮品店", "奶茶店", "咖啡厅", "茶艺馆", "面包店")):
            continue  # 纯饮品/甜品不能替代午餐或晚餐，手动加入仍可保留。
        name = str(restaurant.get("name") or "").strip()
        coord = [restaurant.get("longitude"), restaurant.get("latitude")]
        if not name or name in seen or not _valid_food_coord(coord):
            continue
        distance = _km(f"{center[0]},{center[1]}", f"{coord[0]},{coord[1]}")
        walking = _verified_walking_distance(restaurant, center)
        effective_distance = max(distance, walking / 1000) if walking is not None else distance
        if effective_distance > 5.0:
            continue
        seen.add(name)
        rating = _positive_food_number(restaurant.get("rating"))
        price = _positive_food_number(restaurant.get("price_per_person"))
        distance_score = math.exp(-((effective_distance / 1.5) ** 2))
        rating_score = min(rating, 5.0) / 5.0 if rating else 0.5
        # 便宜且符合总预算的近邻餐厅，不因“舒适档”被排除。
        price_score = min(1.0, target_price / price) if price and target_price else 0.5
        score = 0.7 * distance_score + 0.2 * rating_score + 0.1 * price_score
        if name in used:
            score *= 0.82
        candidates.append({
            **restaurant,
            "longitude": float(coord[0]), "latitude": float(coord[1]),
            "distance_m": round(distance * 1000), "score": round(score, 4),
            "budget_per_meal": target_price,
            "over_meal_budget": bool(price and target_price and price > target_price),
            "walking_distance_m": walking,
            "walking_duration_s": restaurant.get("walking_duration_s") if walking is not None else None,
        })
    # 只将首选加入 used；备选仍可供其他餐点使用。附近只有一家时允许重复，
    # 不能为了跨天去重把用户赶到更远的地方。
    candidates.sort(key=lambda r: (-r["score"], r["distance_m"], r["name"] in used))
    return candidates[:limit]


def _coord_key(coord) -> str:
    return f"{float(coord[0]):.6f},{float(coord[1]):.6f}"


def _verified_walking_distance(restaurant: dict, center) -> float | None:
    # 一个餐厅可能同时出现在多个景点的候选里，不能串用另一锚点的步行距离。
    if restaurant.get("walking_origin") != _coord_key(center):
        return None
    return _positive_food_number(restaurant.get("walking_distance_m"))


def _enrich_food_walking(entries: list[dict], budget_tier=None, meal_budget=None) -> None:
    """每个锚点只核实前八个候选，重复餐点/方案共用查询；失败保留标明的直线距离。"""
    jobs = {}
    for entry in entries:
        center = [entry.get("longitude"), entry.get("latitude")]
        entry["restaurants"] = _pick_restaurants(entry.get("restaurants") or [], "", set(), center,
            budget_tier, limit=8, meal_budget=meal_budget)
        if not _valid_food_coord(center):
            continue
        origin = _coord_key(center)
        for restaurant in entry["restaurants"]:
            dest = _coord_key([restaurant["longitude"], restaurant["latitude"]])
            jobs.setdefault((origin, dest), []).append(restaurant)
    def fetch(pair):
        return walking_route(*pair)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fetch, jobs))
    for pair, route in zip(jobs, results):
        origin, _ = pair
        for restaurant in jobs[pair]:
            restaurant.update(walking_origin=origin,
                walking_distance_m=route.get("distance_m") if route else None,
                walking_duration_s=route.get("duration_s") if route else None)


def _meal_distance_note(meal: str, anchor_name: str, restaurant: dict) -> str:
    note = f"{meal} · 距{anchor_name}约{restaurant['distance_m']}米（直线距离）"
    if restaurant.get("walking_distance_m") is not None and restaurant.get("walking_duration_s"):
        note += f"；步行约{round(restaurant['walking_distance_m'])}米/{math.ceil(restaurant['walking_duration_s'] / 60)}分钟"
    return note


def _positive_food_number(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        return None


def _valid_food_coord(coord) -> bool:
    try:
        lng, lat = float(coord[0]), float(coord[1])
        return math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90 and (lng != 0 or lat != 0)
    except (ValueError, TypeError, IndexError):
        return False


def _to_minutes(value: str) -> int | None:
    match = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _slot_time(schedule: list[dict], kind: str, activity_window: dict | None = None) -> str:
    """根据当天景点时间，找午餐/晚餐的空档时间。"""
    occupied: list[tuple[int, int]] = []
    for item in schedule:
        if (item.get("type") or "") not in ("景点", "活动", "交通", "美食", "餐饮"):
            continue
        match = re.match(
            r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})",
            str(item.get("time") or ""),
        )
        if match:
            start = _to_minutes(match.group(1))
            end = _to_minutes(match.group(2))
            if start is not None and end is not None:
                occupied.append((start, end))

    window = (11 * 60, 14 * 60) if kind == "午餐" else (17 * 60, 20 * 60)
    if isinstance(activity_window, dict):
        try:
            window = (max(window[0], int(activity_window["start_min"])),
                      min(window[1], int(activity_window["end_min"])))
        except (KeyError, ValueError, TypeError):
            pass
    if window[1] - window[0] < 40:
        return ""
    occupied = sorted(o for o in occupied if o[1] > window[0] and o[0] < window[1])

    free_from = window[0]
    gaps: list[tuple[int, int]] = []
    for start, end in occupied:
        start = max(start, window[0])
        end = min(end, window[1])
        if start - free_from >= 40:
            gaps.append((free_from, start))
        free_from = max(free_from, end)
    if window[1] - free_from >= 40:
        gaps.append((free_from, window[1]))
    if not gaps:
        return ""
    preferred = 12 * 60 if kind == "午餐" else 18 * 60
    gap_start, gap_end = min(gaps, key=lambda gap: abs(max(gap[0], min(preferred, gap[1] - 40)) - preferred))
    start = max(gap_start, min(preferred, gap_end - min(60, gap_end - gap_start)))
    end = min(start + 60, gap_end)
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _meal_anchor(schedule: list[dict], time: str, poi_map: dict[str, list[float]]) -> dict | None:
    """用餐前最后一个（或餐后第一个）已排定景点，不拿全日中心代替。"""
    meal_start = _to_minutes(time)
    if meal_start is None:
        return None
    spots: list[tuple[int, int, str, list[float]]] = []
    for item in schedule:
        if item.get("type") not in ("景点", "活动"):
            continue
        name = str(item.get("name") or "").strip()
        coord = [item.get("lng", item.get("longitude")), item.get("lat", item.get("latitude"))]
        if not _valid_food_coord(coord):
            coord = poi_map.get(name)
        if not _valid_food_coord(coord):
            continue
        match = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", str(item.get("time") or ""))
        if not match:
            continue
        start, end = _to_minutes(match[1]), _to_minutes(match[2])
        if start is not None and end is not None:
            spots.append((start, end, name, coord))
    previous = [spot for spot in spots if spot[1] <= meal_start]
    next_spots = [spot for spot in spots if spot[0] >= meal_start]
    if previous:
        _, _, name, coord = max(previous, key=lambda spot: spot[1])
    elif next_spots:
        _, _, name, coord = min(next_spots, key=lambda spot: spot[0])
    else:
        return None
    return {"anchor_name": name, "longitude": float(coord[0]), "latitude": float(coord[1])}


def _has_meal(schedule: list[dict], meal: str) -> bool:
    for item in schedule:
        if item.get("type") not in ("美食", "餐饮"):
            continue
        if meal in f"{item.get('meal') or ''} {item.get('name') or ''} {item.get('note') or ''}":
            return True
        minute = _to_minutes(item.get("time") or "")
        if minute is not None and ((meal == "午餐" and 11 * 60 <= minute < 14 * 60)
                                   or (meal == "晚餐" and 17 * 60 <= minute < 20 * 60)):
            return True
    return False


def _add_food_to_day(
    day: dict,
    food: list[dict],
    district: str,
    center: list[float] | None,
    poi_map: dict[str, list[float]],
    used_restaurants: set[str],
    budget_tier: str | None = None,
    food_by_meal: dict[str, list[dict]] | None = None,
    meal_budget: float | None = None,
) -> dict:
    """第二阶段：给已生成的一天行程补午餐/晚餐，并跨天去重。"""
    schedule = list(day.get("schedule") or [])
    for meal in ("午餐", "晚餐"):
        if _has_meal(schedule, meal):
            continue
        time = _slot_time(schedule, meal, day.get("activity_window"))
        if not time:
            continue
        anchor_info = _meal_anchor(schedule, time, poi_map)
        if not anchor_info:
            continue
        anchor = [anchor_info["longitude"], anchor_info["latitude"]]
        meal_food = food_by_meal.get(meal, []) if food_by_meal is not None else food
        restaurants = _pick_restaurants(
            meal_food, district, used_restaurants, anchor, budget_tier, limit=3,
            meal_budget=meal_budget,
        )
        if restaurants:
            options = []
            for r in restaurants:
                options.append(
                    {
                        "name": r.get("name"),
                        "price": _positive_food_number(r.get("price_per_person")),
                        "link": r.get("poi_detail_url") or r.get("map_url") or r.get("url") or "",
                        "lng": r.get("longitude"),
                        "lat": r.get("latitude"),
                        "map_url": r.get("map_url") or "",
                        "poi_detail_url": r.get("poi_detail_url") or "",
                        "poi_id": r.get("poi_id") or "",
                        "rating": _positive_food_number(r.get("rating")),
                        "distance_m": r.get("distance_m"),
                        "walking_distance_m": r.get("walking_distance_m"),
                        "walking_duration_s": r.get("walking_duration_s"),
                        "walking_origin": r.get("walking_origin"),
                        "cuisine": r.get("cuisine") or "",
                        "score": r.get("score"),
                        "over_meal_budget": r.get("over_meal_budget", False),
                    }
                )
            first = options[0]
            used_restaurants.add(str(first.get("name") or ""))
            schedule.append(
                {
                    "time": time,
                    "type": "美食",
                    **first,
                    "name": first["name"],
                    "meal": meal,
                    "generated_food": True,
                    "selected_option": first["name"],
                    "anchor_name": anchor_info["anchor_name"],
                    "note": _meal_distance_note(meal, anchor_info["anchor_name"], first),
                    "options": options,
                }
            )
    schedule.sort(key=lambda s: (s.get("time") or "99:99"))
    day["schedule"] = schedule
    return day


def _poi_coord_map(search_result: dict) -> dict[str, list[float]]:
    destination = search_result.get("destination") or ""
    poi_map: dict[str, list[float]] = {}
    for poi in search_result.get("poi") or []:
        name = (poi.get("name") or "").strip()
        if not name:
            continue
        lng = poi.get("longitude")
        lat = poi.get("latitude")
        if lng is None or lat is None:
            hit = geocode(name, destination)
            loc = (hit or {}).get("location") or ""
            if loc:
                lng, lat = loc.split(",")
        if lng is not None and lat is not None:
            poi_map[name] = [float(lng), float(lat)]
    return poi_map


def _meal_budget_for_plan(plan: dict, basic: dict | None) -> float | None:
    basic = basic or {}
    total = _positive_food_number(basic.get("total_budget"))
    if total is None:
        return None
    travelers_match = re.search(r"\d+", str(basic.get("travelers") or 1))
    travelers = max(1, int(travelers_match[0])) if travelers_match else 1
    try:
        days = max(1, int(plan.get("days") or basic.get("duration_days") or basic.get("days") or 1))
    except (ValueError, TypeError):
        days = 1
    return round(total * 0.25 / travelers / days / 3, 2)


def refresh_food_for_plan(
    plan: dict,
    search_result: dict | None = None,
    basic: dict | None = None,
    force: bool = True,
    targets: list[dict] | None = None,
) -> dict:
    """景点/时间修改后重新搜索餐饮；保留 user_added/user_selected/locked 餐厅。

    顶层 food 返回已排序的附近候选，food_by_anchor 保留其餐点和景点关联，
    可由编排层合并进 search 后在前端右侧展示。不会为缺少锚点的天塞全城餐厅。
    """
    if not isinstance(plan, dict):
        return plan
    search_result = search_result if isinstance(search_result, dict) else {}
    destination = str(plan.get("destination") or search_result.get("destination") or "").strip()
    if not plan.get("plans") and isinstance(plan.get("blocks"), list):
        grouped: dict[str, dict[int, dict]] = {}
        for block in plan["blocks"]:
            style = block.get("plan_style") or "推荐方案"
            day_num = block.get("day") or 1
            day = grouped.setdefault(style, {}).setdefault(day_num, {
                "day": day_num, "date": block.get("date") or "", "schedule": [],
            })
            if block.get("type") == "酒店":
                day["hotel"], day["hotel_link"] = block.get("name"), block.get("link")
            else:
                day["schedule"].append(dict(block))
        plan["plans"] = [
            {"style": style, "itinerary": list(days.values())}
            for style, days in grouped.items()
        ]
    all_days_with_style = [
        (p.get("style") or "推荐方案", day)
        for p in (plan.get("plans") or [])
        for day in (p.get("itinerary") or [])
    ]
    target_keys = {
        (str(target.get("plan_style") or "推荐方案"), target.get("day"))
        for target in (targets or []) if isinstance(target, dict)
    }
    days_with_style = [
        (style, day) for style, day in all_days_with_style
        if not target_keys or (style, day.get("day")) in target_keys
    ]
    poi_names = {
        str(item.get("name") or "").strip()
        for _, day in days_with_style
        for item in (day.get("schedule") or [])
        if item.get("type") in ("景点", "活动")
    }
    poi_map = _poi_coord_map({
        **search_result, "destination": destination,
        "poi": [poi for poi in (search_result.get("poi") or []) if poi.get("name") in poi_names],
    })
    for _, day in days_with_style:
        for item in day.get("schedule") or []:
            name = str(item.get("name") or "").strip()
            if name not in poi_names or name in poi_map:
                continue
            coord = [item.get("lng", item.get("longitude")), item.get("lat", item.get("latitude"))]
            if _valid_food_coord(coord):
                poi_map[name] = [float(coord[0]), float(coord[1])]
            elif os.getenv("AMAP_KEY") and destination:
                hit = geocode(name, destination)
                if hit and hit.get("location"):
                    coord = hit["location"].split(",")
                    if _valid_food_coord(coord):
                        poi_map[name] = [float(coord[0]), float(coord[1])]
    anchors: list[dict] = []
    for style, day in days_with_style:
        schedule = list(day.get("schedule") or [])
        if force:
            schedule = [
                item for item in schedule
                if item.get("type") not in ("美食", "餐饮")
                or item.get("user_added") or item.get("user_selected") or item.get("locked")
            ]
        day["schedule"] = schedule
        for meal in ("午餐", "晚餐"):
            if _has_meal(schedule, meal):
                continue
            time = _slot_time(schedule, meal, day.get("activity_window"))
            anchor = _meal_anchor(schedule, time, poi_map) if time else None
            if anchor:
                anchors.append({
                    **anchor, "plan_style": style, "day": day.get("day"),
                    "date": day.get("date") or "", "meal": meal, "time": time,
                })
    food_by_anchor: list[dict] = []
    warnings: list[str] = []
    if anchors and destination:
        payload = {
            "destination": destination, "anchors": anchors,
            "food_keyword": (basic or {}).get("food_keyword") or search_result.get("food_keyword"),
            "basic": {**(basic or {}), "days": plan.get("days") or len(all_days_with_style)},
        }
        python = SEARCH_PYTHON
        if sys.platform == "win32":
            python = ROOT / "SearchAgent" / ".venv" / "Scripts" / "python.exe"
        env = dict(os.environ)
        env.pop("__PYVENV_LAUNCHER__", None)
        try:
            proc = subprocess.run(
                [str(python) if python.exists() else sys.executable, str(SEARCH_PY), "--food-nearby"],
                input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
                timeout=max(60, min(300, len(anchors) * 30)), env=env,
            )
            nearby = json.loads(proc.stdout or "{}")
            if proc.returncode != 0 or nearby.get("error"):
                warnings.append("附近餐厅查询暂不可用，可稍后重试。")
            elif isinstance(nearby.get("food_by_anchor"), list):
                food_by_anchor = nearby["food_by_anchor"]
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            warnings.append("附近餐厅查询暂不可用，可稍后重试。")
    meal_budget = _meal_budget_for_plan(plan, basic)
    tiers = (basic or {}).get("budget_tiers") or []
    budget_tier = tiers[0] if isinstance(tiers, list) and tiers else None
    _enrich_food_walking(food_by_anchor, budget_tier, meal_budget)
    all_food: list[dict] = []
    for entry in food_by_anchor:
        coord = [entry.get("longitude"), entry.get("latitude")]
        entry["restaurants"] = _pick_restaurants(
            entry.get("restaurants") or [], "", set(), coord, budget_tier,
            limit=25, meal_budget=meal_budget,
        )
        for restaurant in entry["restaurants"]:
            all_food.append({
                **restaurant, "day": entry.get("day"), "date": entry.get("date"),
                "meal": entry.get("meal"), "time": entry.get("time"),
                "plan_style": entry.get("plan_style"), "anchor_name": entry.get("anchor_name"),
                "link": restaurant.get("poi_detail_url") or restaurant.get("map_url") or restaurant.get("url") or "",
            })
        if not entry["restaurants"]:
            warnings.append(f"第{entry.get('day')}天{entry.get('meal')}在景点5公里内暂无可核实餐厅，已留出就餐空档。")
    used_by_style: dict[str, set[str]] = {}
    for style, day in days_with_style:
        by_meal = {
            entry.get("meal"): entry.get("restaurants") or []
            for entry in food_by_anchor
            if entry.get("day") == day.get("day") and entry.get("plan_style", style) == style
        }
        _add_food_to_day(day, [], "", None, poi_map, used_by_style.setdefault(style, set()), budget_tier,
                         food_by_meal=by_meal, meal_budget=meal_budget)
    if target_keys:
        food_by_anchor = [
            entry for entry in (plan.get("food_by_anchor") or [])
            if (entry.get("plan_style") or "推荐方案", entry.get("day")) not in target_keys
        ] + food_by_anchor
        all_food = [
            entry for entry in (plan.get("food") or [])
            if (entry.get("plan_style") or "推荐方案", entry.get("day")) not in target_keys
        ] + all_food
    plan["food_by_anchor"] = food_by_anchor
    plan["food"] = all_food
    plan["food_warnings"] = list(dict.fromkeys(warnings))
    search_result["food"] = all_food
    search_result["food_by_anchor"] = food_by_anchor
    search_result["food_search_pending"] = False
    return plan


_HOTEL_PRICE_BANDS = {
    "经济": (0.0, 250.0),
    "舒适": (250.0, 600.0),
    "豪华": (600.0, 3000.0),
}


def _hotel_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    if isinstance(value, str):
        text = value.strip().lstrip("¥￥").replace(",", "")
        try:
            number = float(text)
            return number if math.isfinite(number) and number >= 0 else None
        except ValueError:
            return None
    return None


def _hotel_in_budget(hotel: dict, budget_tier: str | None) -> bool:
    if not budget_tier or budget_tier not in _HOTEL_PRICE_BANDS:
        return True
    low, high = _HOTEL_PRICE_BANDS[budget_tier]
    price = _hotel_price(hotel.get("price"))
    return price is not None and low <= price <= high


def _hotel_coord(hotel: dict, destination: str) -> list[float] | None:
    lng = hotel.get("longitude")
    lat = hotel.get("latitude")
    if lng is not None and lat is not None:
        try:
            coord = [float(lng), float(lat)]
            if _valid_food_coord(coord):
                return coord
        except (ValueError, TypeError):
            pass
    hit = geocode(str(hotel.get("name") or ""), destination)
    loc = (hit or {}).get("location") or ""
    if loc and "," in loc:
        try:
            coord = [float(part) for part in loc.split(",")[:2]]
            if _valid_food_coord(coord):
                return coord
        except (ValueError, TypeError):
            return None
    return None


def _pick_hotel(
    hotels: list[dict],
    center: list[float] | None,
    destination: str,
    used_hotels: set[str],
    budget_tier: str | None = None,
) -> dict | None:
    candidates = [
        h for h in hotels
        if (h.get("name") or "").strip() and (h.get("name") or "").strip() not in used_hotels
        and _hotel_in_budget(h, budget_tier)
    ]
    if not candidates:
        return None
    if center:
        def distance(h: dict) -> float:
            coord = _hotel_coord(h, destination)
            if not coord:
                return float("inf")
            return _km(f"{center[0]},{center[1]}", f"{coord[0]},{coord[1]}")

        candidates.sort(key=distance)
    else:
        candidates.sort(key=lambda h: (h.get("star") is None, -(len(h.get("star") or ""))))
    return candidates[0]


def _itinerary_center(
    day_plans: list[dict], poi_map: dict[str, list[float]]
) -> list[float] | None:
    """返回全部天景点坐标的质心（相对中心），用于统一选酒店。"""
    lngs: list[float] = []
    lats: list[float] = []
    for day in day_plans:
        for item in day.get("schedule") or []:
            if (item.get("type") or "") != "景点":
                continue
            coord = poi_map.get((item.get("name") or "").strip())
            if _valid_food_coord(coord):
                lngs.append(coord[0])
                lats.append(coord[1])
    if not lngs:
        return None
    return [sum(lngs) / len(lngs), sum(lats) / len(lats)]


def _cluster_by_radius(
    names: list[str], poi_map: dict[str, list[float]], radius_km: float = 10.0
) -> dict[str, int]:
    """把景点按半径 radius_km 聚成 block（贪心：依次以未分类景点为中心并入半径内景点）。"""
    block_of: dict[str, int] = {}
    located = {
        name: poi_map[name]
        for name in names
        if _valid_food_coord(poi_map.get(name))
    }
    next_bid = 0
    for name in names:
        if name in block_of:
            continue
        block_of[name] = next_bid
        next_bid += 1
        coord = located.get(name)
        if coord is None:
            continue
        for other in names:
            if other in block_of:
                continue
            c2 = located.get(other)
            if c2 is None:
                continue
            if _km(f"{coord[0]},{coord[1]}", f"{c2[0]},{c2[1]}") <= radius_km:
                block_of[other] = block_of[name]
    return block_of


def _block_centroids(
    name_to_block: dict[str, int], poi_map: dict[str, list[float]]
) -> dict[int, list[float]]:
    """计算每个 block 的坐标质心。"""
    lngs: dict[int, list[float]] = {}
    lats: dict[int, list[float]] = {}
    for name, b in name_to_block.items():
        coord = poi_map.get(name)
        if not _valid_food_coord(coord):
            continue
        lngs.setdefault(b, []).append(coord[0])
        lats.setdefault(b, []).append(coord[1])
    return {
        b: [sum(lngs[b]) / len(lngs[b]), sum(lats[b]) / len(lats[b])]
        for b in lngs
        if lngs[b]
    }


def _add_hotel_to_day(
    day: dict,
    hotels: list[dict],
    center: list[float] | None,
    destination: str,
    used_hotels: set[str],
    budget_tier: str | None = None,
) -> dict:
    hotel = _pick_hotel(hotels, center, destination, used_hotels, budget_tier)
    day["hotel"] = (hotel.get("name") or "") if hotel else ""
    if hotel:
        used_hotels.add(str(hotel.get("name") or ""))
    return day


def _add_first_day_checkin(day_plan: dict) -> dict:
    """第一天先办理入住：在去程交通之后、景点之前插入酒店入住项。"""
    if (day_plan.get("day") or 0) != 1:
        return day_plan
    hotel = str(day_plan.get("hotel") or "").strip()
    if not hotel or any(x in hotel for x in ("无住宿", "返程", "不住宿", "无需住宿")):
        return day_plan

    schedule = list(day_plan.get("schedule") or [])
    if any((s.get("type") or "") == "酒店" for s in schedule):
        return day_plan

    start = None
    for item in schedule:
        if (item.get("type") or "") == "交通" and "去程" in str(item.get("name") or ""):
            rng = _schedule_range(item)
            if rng:
                start = rng[1]
            break
    if start is None:
        window = day_plan.get("activity_window") or {}
        start = int(window.get("start_min") or (8 * 60 + 30))
    if start < 0 or start >= 1440:
        return day_plan
    start = max(0, min(start, 1440))
    end = min(start + 30, 1440)

    schedule.append({
        "type": "酒店",
        "name": hotel,
        "time": f"{_clock(start)}-{_clock(end)}",
        "note": "办理入住，放下行李",
    })
    schedule.sort(key=lambda s: _to_minutes(s.get("time") or "") if _to_minutes(s.get("time") or "") is not None else 1440)
    day_plan["schedule"] = schedule
    return day_plan


def _search_hotels(
    destination: str,
    check_in: str | None,
    check_out: str | None,
    max_price: float | None,
    limit: int = 30,
) -> list[dict]:
    """调用 SearchAgent 补搜指定预算的酒店。"""
    if not destination:
        return []
    python = SEARCH_PYTHON
    if sys.platform == "win32":
        python = ROOT / "SearchAgent" / ".venv" / "Scripts" / "python.exe"
    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)
    payload = {
        "destination": destination,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "max_price": max_price,
        "limit": limit,
    }
    try:
        proc = subprocess.run(
            [str(python) if python.exists() else sys.executable, str(SEARCH_PY), "--hotels"],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        data = json.loads(proc.stdout or "{}")
        hotels = data.get("hotels")
        return hotels if isinstance(hotels, list) else []
    except Exception:
        return []


def _ensure_hotels(plan: dict, search_result: dict) -> dict:
    """全局修改后给每天补酒店。"""
    poi_map = _poi_coord_map(search_result)
    hotels = search_result.get("hotels") or []
    destination = search_result.get("destination") or ""
    used: set[str] = set()
    days = plan.get("days") or 0
    for p in plan.get("plans") or []:
        for it in p.get("itinerary") or []:
            if (it.get("hotel") or "").strip():
                continue
            if it.get("day") == days:
                it["hotel"] = "当晚返程，无住宿"
            else:
                _add_hotel_to_day(it, hotels, None, destination, used)
    plan["blocks"] = blockify(plan)
    return plan


def build_plan(
    search_result: dict,
    profile: dict | None = None,
    preferences: dict | None = None,
    recent_trips: list | None = None,
    basic: dict | None = None,
    feedback: str | None = None,
    modify: dict | None = None,
    recent_trip_summary: str | None = None,
    skip_trip_summary: bool = False,
) -> dict:
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY"), "max_retries": 0}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    meta = _build_meta(search_result)
    destination = search_result.get("destination") or ""
    days = meta.get("days") or 0

    transport = _select_transport(search_result, basic)

    # 为每个景点按画像/偏好/本次旅行信息计算匹配度，供模型和最终输出使用。
    for poi in (search_result.get("poi") or []):
        if isinstance(poi, dict):
            poi["match_score"] = _score_poi_match(poi, profile, preferences, basic)

    search_result = _filter_far_pois(search_result, destination)

    styles = list((profile or {}).get("travel_style") or []) + list((basic or {}).get("travel_style") or [])
    pace = str((preferences or {}).get("pace") or (basic or {}).get("pace") or "")

    selected_search = dict(search_result)
    for key, kind in (("trains", "train"), ("flights", "flight")):
        selected_search[key] = [record for record in transport.values() if isinstance(record, dict) and record.get("kind") == kind]
    context: dict = {"search": _trim_search(selected_search), "selected_transport": transport}
    min_spots, max_spots = _pace_spots(pace, styles)
    context["min_spots"] = min_spots
    context["max_spots"] = max_spots
    if profile:
        context["user_profile"] = profile
    if preferences:
        context["preferences"] = preferences
    if recent_trips:
        context["recent_trips"] = recent_trips
    if recent_trip_summary:
        context["recent_trip_summary"] = recent_trip_summary
    if basic:
        context["basic"] = basic
    if feedback:
        context["feedback"] = feedback
    if modify:
        context["modify"] = modify

    poi_map = _poi_coord_map(search_result)
    day_assignments, name_to_block = _assign_days_by_score(
        search_result,
        days,
        min_spots,
        max_spots,
        travel_categories=styles,
        poi_map=poi_map,
        radius_km=10.0,
    )
    day_to_block: dict[int, int] = {}
    for da in day_assignments:
        counts: dict[int, int] = {}
        for name in da.get("names") or []:
            b = name_to_block.get(name)
            if b is not None:
                counts[b] = counts.get(b, 0) + 1
        if counts:
            day_to_block[da["day"]] = max(counts, key=lambda b: counts[b])
    with ThreadPoolExecutor(max_workers=min(max(days, 1), 8)) as executor:
        day_plans = list(
            executor.map(
                lambda da: _build_day_plan(
                    client, context, da, days, meta.get("start_date") or ""
                ),
                day_assignments,
            )
        )
    day_plans = sorted(day_plans, key=lambda d: d.get("day") or 0)
    for index, day_plan in enumerate(day_plans):
        assignment = day_assignments[index] if index < len(day_assignments) else {"day": index + 1, "names": [], "min_spots": min_spots, "max_spots": max_spots}
        date_ = (date.fromisoformat(meta["start_date"]) + timedelta(days=(assignment["day"] or index + 1) - 1)).isoformat()
        day_plans[index] = _enforce_day_schedule(day_plan, assignment, date_, transport, search_result)
    budget_tiers = (basic or {}).get("budget_tiers") or []
    budget_tier = budget_tiers[0] if isinstance(budget_tiers, list) and budget_tiers else None
    hotels = search_result.get("hotels") or []
    supplemental_hotels = []
    # 优先在已搜索的酒店里找对应预算且有坐标的；找不到再调 SearchAgent 补搜。
    budget_hotels = [
        h for h in hotels
        if _hotel_in_budget(h, budget_tier) and _hotel_coord(h, destination)
    ]
    if not budget_hotels and budget_tier in _HOTEL_PRICE_BANDS:
        extra = _search_hotels(
            destination,
            meta.get("start_date") or "",
            meta.get("end_date") or "",
            _HOTEL_PRICE_BANDS[budget_tier][1],
            limit=30,
        )
        if extra:
            seen_names = {str(h.get("name") or "") for h in hotels}
            for h in extra:
                name = str(h.get("name") or "")
                if name and name not in seen_names:
                    hotels.append(h)
                    supplemental_hotels.append(h)
                    seen_names.add(name)
    search_result["hotels"] = hotels  # Also expose supplemental quotes to price attachment.
    # 每个 10km block 就近安排一个酒店，同 block 的天共用同一酒店。
    block_centroids = _block_centroids(name_to_block, poi_map)
    block_hotels: dict[int, str] = {}
    used_hotels: set[str] = set()
    for b in sorted(block_centroids):
        hotel = _pick_hotel(hotels, block_centroids[b], destination, used_hotels, budget_tier)
        if hotel:
            block_hotels[b] = hotel.get("name") or ""
            used_hotels.add(str(hotel.get("name") or ""))
    for day_plan in day_plans:
        day_num = day_plan.get("day")
        if day_num == days:
            day_plan["hotel"] = ""
        else:
            b = day_to_block.get(day_num)
            day_plan["hotel"] = block_hotels.get(b) if b is not None else ""
    for day_plan in day_plans:
        if (day_plan.get("day") or 0) == 1:
            _add_first_day_checkin(day_plan)

    result = meta
    result["selected_transport"] = transport
    warnings = []
    origin = (basic or {}).get("origin") or search_result.get("origin")
    if origin and origin != destination:
        for key, label, target_day in (("outbound", "去程", 1), ("inbound", "返程", days)):
            if not transport.get(key):
                warnings.append(f"{label}未找到可核实班次，交通时间和票价待确认，景点时间为建议。")
                if day_plans:
                    day_plan = next((day for day in day_plans if day.get("day") == target_day), day_plans[-1])
                    day_plan["schedule"].append({"type": "交通", "name": f"{label}交通待确认", "time": "", "note": warnings[-1], "direction": "去" if key == "outbound" else "回"})
    result["warnings"] = warnings
    result["plans"] = [
        {
            "style": "推荐方案",
            "summary": f"{destination} {days} 日游",
            "itinerary": day_plans,
        }
    ]
    result = _dedupe_poi_names(result, search_result)
    result = _reorder_by_proximity(result, result.get("destination") or "")
    # 最终地点名称确定后再校准源坐标，避免顺序校正沿用旧景点坐标。
    for day_plan in day_plans:
        for item in day_plan.get("schedule") or []:
            coord = poi_map.get(item.get("name"))
            if item.get("type") == "景点" and _valid_food_coord(coord):
                item.update({"lng": coord[0], "lat": coord[1]})
    result = refresh_food_for_plan(result, search_result, basic)
    if supplemental_hotels:
        result["source_updates"] = {"hotels": supplemental_hotels}
    return _backfill_links(result, search_result)


def _food_choices_for_edit(block: dict, full_blocks: list[dict], search: dict, basic: dict, instruction: str) -> tuple[list[dict], dict | None]:
    """Scope a meal edit to its neighboring attractions, never another day's city-wide picks."""
    entries = [entry for entry in search.get("food_by_anchor") or []
        if entry.get("day") == block.get("day")
        and (not entry.get("plan_style") or entry.get("plan_style") == block.get("plan_style"))
        and (not block.get("meal") or entry.get("meal") == block.get("meal"))]
    anchor = next((entry for entry in entries if entry.get("time") == block.get("time")), entries[0] if entries else None)
    if not anchor:
        schedule = [item for item in full_blocks if item.get("day") == block.get("day") and item.get("plan_style") == block.get("plan_style")]
        poi_map = {item.get("name"): [item.get("longitude"), item.get("latitude")]
            for item in search.get("poi") or [] if item.get("longitude") is not None and item.get("latitude") is not None}
        anchor = _meal_anchor(schedule, block.get("time") or "", poi_map)
    if not anchor:
        return [], None
    pool = list(anchor.get("restaurants") or []) + list(search.get("food") or [])
    for option in block.get("options") or []:
        pool.append({**option, "longitude": option.get("lng"), "latitude": option.get("lat"),
            "price_per_person": option.get("price"), "poi_detail_url": option.get("link")})
    tiers = basic.get("budget_tiers") or []
    budget = _meal_budget_for_plan({"days": basic.get("duration_days") or max([int(item.get("day") or 1) for item in full_blocks] or [1])}, basic)
    enriched = [{**anchor, "restaurants": pool}]
    _enrich_food_walking(enriched, tiers[0] if tiers else None, budget)
    choices = _pick_restaurants(enriched[0]["restaurants"], "", set(), [anchor.get("longitude"), anchor.get("latitude")], tiers[0] if tiers else None, limit=30, meal_budget=budget)
    if any(word in instruction for word in ("不要太远", "近一点", "近一些", "步行", "就近")):
        choices = [item for item in choices if max(item["distance_m"], item.get("walking_distance_m") or 0) <= 1500]
    if any(word in instruction for word in ("便宜", "省钱", "经济", "降预算")):
        original_price = _positive_food_number(block.get("unit_price")) or _positive_food_number(next((o.get("price") for o in block.get("options") or [] if o.get("name") == block.get("name")), block.get("price")))
        cheaper = [item for item in choices if _positive_food_number(item.get("price_per_person")) and (not original_price or float(item["price_per_person"]) < original_price)]
        if cheaper:
            choices = cheaper
    if any(word in instruction for word in ("换一家", "别家", "不要这家", "换个")):
        alternatives = [item for item in choices if item.get("name") != block.get("name")]
        if alternatives:
            choices = alternatives
    return choices, anchor


def modify_blocks(
    blocks: list[dict],
    instruction: str,
    search_result: dict | None = None,
    profile: dict | None = None,
    basic: dict | None = None,
    full_blocks: list[dict] | None = None,
) -> dict:
    """局部修改：只修改选中的 block，输出改完后的 blocks 列表。"""
    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    context: dict = {"blocks": blocks, "instruction": instruction}
    food_choices = {
        block.get("id"): _food_choices_for_edit(block, full_blocks or blocks, search_result or {}, basic or {}, instruction)
        for block in blocks if block.get("type") in ("美食", "餐饮")
    }
    if full_blocks:
        context["full_blocks"] = full_blocks
    if search_result:
        context["search"] = _trim_search(search_result)
        if food_choices:
            context["search"]["food"] = [candidate for choices, _ in food_choices.values() for candidate in choices]
    if profile:
        context["user_profile"] = profile
    if basic:
        context["basic"] = basic

    resp = _chat_completion(client,
        model=os.getenv("OPENAI_MODEL", "qwen3.8-27b"),
        messages=[
            {"role": "system", "content": MODIFY_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        max_tokens=3000,
        timeout=90,
        **_model_options(),
    )
    content = (resp.choices[0].message.content or "{}").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    response = json.loads(content)
    edited = merge_block_edits(blocks, response.get("blocks") or [])
    sources = [
        item for key in ("poi", "hotels", "food", "events")
        for item in (search_result or {}).get(key) or [] if isinstance(item, dict)
    ]
    by_name = {str(item.get("name") or item.get("title") or "").strip(): item for item in sources}
    original_by_id = {block.get("id"): block for block in blocks}
    for block in edited:
        original = original_by_id.get(block.get("id")) or {}
        if block.get("id") in food_choices:
            choices, anchor = food_choices[block["id"]]
            if not choices:
                block.clear()
                block.update(original)
                block["note"] = "附近暂未找到符合这次要求的餐厅，先保留原推荐。"
                continue
            chosen = next((item for item in choices if item.get("name") == block.get("name")), choices[0])
            options = [{"name": item["name"], "price": item.get("price_per_person"),
                "lng": item["longitude"], "lat": item["latitude"],
                "link": item.get("poi_detail_url") or item.get("map_url") or item.get("url") or "",
                "rating": item.get("rating"), "distance_m": item.get("distance_m"),
                "walking_distance_m": item.get("walking_distance_m"),
                "walking_duration_s": item.get("walking_duration_s"), "walking_origin": item.get("walking_origin"),
                "distance_km": round(item["distance_m"] / 1000, 1), "cuisine": item.get("cuisine") or ""} for item in choices[:3]]
            selected = next((option for option in options if option["name"] == chosen["name"]), None)
            if not selected:
                selected = {"name": chosen["name"], "price": chosen.get("price_per_person"),
                    "lng": chosen["longitude"], "lat": chosen["latitude"],
                    "link": chosen.get("poi_detail_url") or chosen.get("map_url") or "",
                    "rating": chosen.get("rating"), "distance_m": chosen["distance_m"],
                    "walking_distance_m": chosen.get("walking_distance_m"),
                    "walking_duration_s": chosen.get("walking_duration_s"), "walking_origin": chosen.get("walking_origin")}
                options.insert(0, selected)
            for key in ("unit_price", "price_basis", "price_known", "price_source", "poi_id"):
                block.pop(key, None)
            block.update({**selected, "options": options, "selected_option": chosen["name"], "user_selected": False,
                "note": _meal_distance_note(original.get("meal") or "用餐", anchor.get("anchor_name") or "相邻景点", chosen)})
            continue
        if block.get("name") == original.get("name"):
            continue
        # 模型有时照抄全部旧字段；更换地点时清除旧地理信息再从检索结果回填。
        for field in ("lng", "lat", "poi_id", "link", "map_url", "options", "selected_option", "rating", "distance_m", "distance_km", "walking_distance_m", "walking_duration_s", "walking_origin", "price", "price_basis", "unit_price", "price_known", "price_source"):
            block.pop(field, None)
        source = by_name.get(str(block.get("name") or "").strip()) or {}
        for target, key in (("lng", "longitude"), ("lat", "latitude"), ("poi_id", "poi_id"), ("rating", "rating")):
            if source.get(key) is not None:
                block[target] = source[key]
        block["link"] = source.get("poi_detail_url") or source.get("map_url") or source.get("url") or ""
        source_price = source.get("price_per_person") if source.get("price_per_person") is not None else source.get("price")
        if source_price is not None:
            block["price"] = source_price
    return {"blocks": edited}


def classify_modify(client: OpenAI, instruction: str, blocks: list[dict]) -> dict:
    brief = [
        {
            "id": b.get("id"),
            "day": b.get("day"),
            "type": b.get("type"),
            "name": b.get("name"),
        }
        for b in blocks
    ]
    resp = _chat_completion(client,
        model=os.getenv("OPENAI_MODEL", "qwen3.8-27b"),
        messages=[
            {"role": "system", "content": CLASSIFY_MODIFY_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"instruction": instruction, "blocks": brief}, ensure_ascii=False
                ),
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=1000,
        timeout=60,
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
    return {
        "mode": result.get("mode") or "global",
        "targets": result.get("targets") or [],
    }


def _modify_block_local(
    plan: dict,
    modify: dict,
    search_result: dict | None = None,
    profile: dict | None = None,
    basic: dict | None = None,
) -> dict:
    """block 修改：只处理受影响的 day，LLM 局部输出（快），再合并回原 plan。"""
    block_map = {b.get("id"): b for b in (plan.get("blocks") or [])}
    affected: set[tuple] = set()
    for bid in modify.get("block_ids") or []:
        b = block_map.get(bid) or {}
        if b.get("plan_style") and b.get("day") is not None:
            affected.add((b.get("plan_style"), b.get("day")))

    if not affected:
        return plan

    days_to_modify: list[dict] = []
    for p in plan.get("plans") or []:
        style = p.get("style")
        for it in p.get("itinerary") or []:
            if (style, it.get("day")) in affected:
                days_to_modify.append(it)

    targets = []
    for bid in modify.get("block_ids") or []:
        b = block_map.get(bid) or {}
        targets.append(
            {"name": b.get("name"), "type": b.get("type"), "day": b.get("day")}
        )

    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)
    context: dict = {
        "days": days_to_modify,
        "modify": {"targets": targets, "instruction": modify.get("instruction", "")},
    }
    if search_result:
        context["search"] = _trim_search(search_result)
    if profile:
        context["user_profile"] = profile
    if basic:
        context["basic"] = basic

    resp = _chat_completion(client,
        model=os.getenv("OPENAI_MODEL", "qwen3.8-27b"),
        messages=[
            {"role": "system", "content": BLOCK_MODIFY_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        timeout=120,
    )
    content = (resp.choices[0].message.content or "{}").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    modified_days = json.loads(content).get("days") or []

    # 按顺序合并回 plan
    modified_idx = 0
    for p in plan.get("plans") or []:
        style = p.get("style")
        new_itinerary = []
        for it in p.get("itinerary") or []:
            if (style, it.get("day")) in affected:
                if modified_idx < len(modified_days):
                    new_itinerary.append(modified_days[modified_idx])
                    modified_idx += 1
                else:
                    new_itinerary.append(it)
            else:
                new_itinerary.append(it)
        p["itinerary"] = new_itinerary
    return plan


_GLOBAL_PRICE_FIELDS = ("price", "unit_price", "price_known", "price_basis", "price_source")


def _global_quote_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def _global_search_quotes(search: dict) -> dict:
    """Only catalogue exact names from retrieved resources, never model output."""
    quotes = {}
    categories = {"hotels": "酒店", "food": "美食", "poi": "景点", "events": "活动", "promotions": "活动", "flights": "交通", "trains": "交通"}
    for category, kind in categories.items():
        items = search.get(category) or []
        if isinstance(items, dict):
            items = [item for batch in items.values() if isinstance(batch, list) for item in batch]
        if not isinstance(items, list):
            continue
        for source in items:
            if not isinstance(source, dict):
                continue
            name = str(source.get("name") or source.get("title") or
                       (str(source.get("airline") or source.get("transport") or "") + str(source.get("flight_no") or source.get("train_no") or ""))).strip()
            price = next((_global_quote_number(source.get(field)) for field in ("price_per_person", "price", "ticketPrice", "ticket_price")
                          if _global_quote_number(source.get(field)) is not None), None)
            if price is None and source.get("free") is True:
                price = 0.0
            if name and price is not None:
                quotes[(kind, name)] = (price, "group" if kind == "酒店" else "per_person")
    return quotes


def _global_user_quote(name: str, kind: str, instruction: str) -> tuple | None:
    """A trip budget is not an item quote; require its exact name and a unit amount."""
    if not name:
        return None
    named = re.escape(name)
    amount = r"(?P<amount>-?\d+(?:\.\d+)?)(?P<scale>[万千]?)"
    label = r"(?P<label>人均|每人|每晚|每间|门票|票价|房价|总价|价格|报价)?"
    match = re.search(named + r"[的，,：:\s]{0,4}(?:实际)?" + label + r"(?:改为|改成|调整为|更新为|确认为|为|是|按|只要|仅需)?\s*" + amount + r"\s*(?:元|块)", instruction)
    if not match:
        if re.search(named + r"[的，,：:\s]{0,4}(?:门票|票价|价格|报价)?(?:为|是)?免费", instruction):
            return 0.0, "group" if kind == "酒店" else "per_person"
        return None
    price = _global_quote_number(float(match["amount"]) * {None: 1, "": 1, "千": 1000, "万": 10000}[match["scale"]])
    if price is None:
        return None
    basis = "group" if match["label"] in ("每晚", "每间", "总价") or (kind == "酒店" and match["label"] not in ("人均", "每人")) else "per_person"
    return price, basis


def _guard_global_plan_quotes(result: dict, original: dict, search: dict, instruction: str) -> dict:
    """Discard invented model prices and protect explicitly chosen meal/hotel records."""
    sources = _global_search_quotes(search)
    originals = []
    for style in original.get("plans") or []:
        for day in style.get("itinerary") or []:
            for item in day.get("schedule") or []:
                originals.append((str(style.get("style") or "推荐方案"), int(day.get("day") or 1), item))
    by_id = {str(item["id"]): (style, day, item) for style, day, item in originals if item.get("id")}
    seen = set()
    protected_flags = ("user_selected", "user_added", "locked")
    for style in result.get("plans") or []:
        style_name = str(style.get("style") or "推荐方案")
        for day in style.get("itinerary") or []:
            day_number = int(day.get("day") or 1)
            for item in day.get("schedule") or []:
                previous = by_id.get(str(item.get("id")))
                if previous and previous[0] != style_name:
                    previous = None
                if previous is None:
                    previous = next(((old_style, old_day, old) for old_style, old_day, old in originals
                                     if old_style == style_name and old_day == day_number
                                     and old.get("name") == item.get("name") and old.get("type") == item.get("type")), None)
                old = previous[2] if previous else {}
                if previous:
                    seen.add(id(old))
                locked = old.get("type") in ("美食", "餐饮", "酒店") and any(old.get(flag) for flag in protected_flags)
                if locked:
                    # The model may change the surrounding calendar but may not
                    # change a user's chosen restaurant/hotel or its quoted data.
                    time = item.get("time") or old.get("time")
                    item.clear()
                    item.update(deepcopy(old), day=day_number, date=day["date"], plan_style=style_name, time=time)
                    manual = _global_user_quote(str(old.get("name") or ""), old.get("type"), instruction)
                    if manual is not None:
                        item.update(price=manual[0], unit_price=manual[0], price_basis=manual[1], price_known=True, price_source="manual")
                        for option in item.get("options") or []:
                            if isinstance(option, dict) and option.get("name") == old.get("name"):
                                option["price"] = manual[0]
                    if old.get("type") == "酒店":
                        day.update(hotel=old.get("name") or "", hotel_link=old.get("link") or "")
                    continue
                for flag in protected_flags:
                    if flag in old and old.get("name") == item.get("name"):
                        item[flag] = old[flag]
                    else:
                        item.pop(flag, None)
                for field in ("price_per_person", "ticketPrice", "ticket_price", "cost"):
                    item.pop(field, None)
                same_place = bool(old) and old.get("name") == item.get("name")
                if same_place and old.get("type"):
                    item["type"] = old["type"]
                manual = _global_user_quote(str(item.get("name") or "").strip(), item.get("type") or "活动", instruction)
                old_known = same_place and old.get("price_known") is not False and (
                    _global_quote_number(old.get("unit_price")) is not None
                    or (old.get("price_known") is True and _global_quote_number(old.get("price")) is not None))
                if old_known and manual is None:
                    for field in _GLOBAL_PRICE_FIELDS:
                        if field in old:
                            item[field] = deepcopy(old[field])
                        else:
                            item.pop(field, None)
                    if "options" in old:
                        item["options"] = deepcopy(old["options"])
                    continue
                for field in _GLOBAL_PRICE_FIELDS:
                    item.pop(field, None)
                kind = "美食" if item.get("type") == "餐饮" else item.get("type") or "活动"
                quote = manual or sources.get((kind, str(item.get("name") or "").strip()))
                if quote is not None:
                    item.update(price=quote[0], unit_price=quote[0], price_basis=quote[1], price_known=True,
                                price_source="manual" if manual is not None else "search")
                    if manual is not None and kind in ("美食", "酒店"):
                        item["user_selected"] = True
                else:
                    item.update(price=None, unit_price=None, price_known=False,
                                price_basis="group" if kind == "酒店" else "per_person", price_source="unknown")
                for option in item.get("options") or []:
                    if not isinstance(option, dict):
                        continue
                    option_name = str(option.get("name") or "").strip()
                    old_option = next((candidate for candidate in old.get("options") or []
                                       if isinstance(candidate, dict) and candidate.get("name") == option_name), {})
                    quote = _global_user_quote(option_name, kind, instruction) or sources.get((kind, option_name))
                    if quote is None and _global_quote_number(old_option.get("price")) is not None:
                        quote = (_global_quote_number(old_option["price"]), "group" if kind == "酒店" else "per_person")
                    option["price"] = quote[0] if quote is not None else None
    # A budget-only global edit must not silently remove a manually selected meal
    # or hotel. Shortened trips may still remove records beyond the new last day.
    for style_name, day_number, old in originals:
        if old.get("type") not in ("美食", "餐饮", "酒店") or not any(old.get(flag) for flag in protected_flags) or id(old) in seen:
            continue
        target_style = next((style for style in result.get("plans") or [] if str(style.get("style") or "推荐方案") == style_name), None)
        target_day = next((day for day in (target_style or {}).get("itinerary") or [] if int(day.get("day") or 1) == day_number), None)
        if not target_day:
            continue
        schedule = target_day.get("schedule") or []
        schedule = [item for item in schedule if not (item.get("type") == old.get("type") and
                    (old.get("type") == "酒店" or item.get("time") == old.get("time")))]
        restored = {**deepcopy(old), "day": day_number, "date": target_day["date"], "plan_style": style_name}
        manual = _global_user_quote(str(old.get("name") or ""), old.get("type"), instruction)
        if manual is not None:
            restored.update(price=manual[0], unit_price=manual[0], price_basis=manual[1], price_known=True, price_source="manual")
        schedule.append(restored)
        schedule.sort(key=lambda item: (item.get("type") == "酒店", _to_minutes(item.get("time") or "") or 1440))
        target_day["schedule"] = schedule
        if old.get("type") == "酒店":
            target_day.update(hotel=old.get("name") or "", hotel_link=old.get("link") or "")
    return result


def _align_modified_trip(result: dict, trip_context: dict, original: dict) -> dict:
    """Keep calendar dates and protected trip parameters independent of model text."""
    aligned = deepcopy(result)
    start = date.fromisoformat(trip_context["start_date"])
    days = trip_context["days"]
    original_styles = {p.get("style") or "推荐方案": p for p in original.get("plans") or []}
    proposed = {p.get("style") or "推荐方案": p for p in aligned.get("plans") or [] if isinstance(p, dict)}
    plans = []
    for style, previous in original_styles.items():
        current = proposed.get(style)
        if current is None and len(original_styles) == 1 and len(proposed) == 1:
            current = next(iter(proposed.values()))
        current = current or previous
        by_day = {}
        for day in current.get("itinerary") or []:
            if not isinstance(day, dict):
                continue
            try:
                day_number = int(day.get("day"))
            except (TypeError, ValueError):
                continue
            if 1 <= day_number <= days:
                by_day[day_number] = day
        previous_days = {int(day.get("day") or 1): day for day in previous.get("itinerary") or []}
        itinerary = []
        for day_number in range(1, days + 1):
            day = deepcopy(by_day.get(day_number) or previous_days.get(day_number) or {
                "theme": "新增旅行日", "schedule": [],
                "note": "新增的一天可从候选列表加入景点，也可以继续描述想去的地方。",
            })
            day["day"] = day_number
            day["date"] = (start + timedelta(days=day_number - 1)).isoformat()
            schedule = day.get("schedule")
            day["schedule"] = [item for item in schedule if isinstance(item, dict)] if isinstance(schedule, list) else []
            for item in day["schedule"]:
                item.update(day=day_number, date=day["date"], plan_style=style)
            itinerary.append(day)
        plans.append({**current, "style": style, "itinerary": itinerary})
    aligned.update(destination=original.get("destination") or aligned.get("destination") or "",
                   start_date=trip_context["start_date"], end_date=trip_context["end_date"],
                   days=days, basic=deepcopy(trip_context["basic"]), plans=plans,
                   trip_context=deepcopy(trip_context))
    aligned.pop("blocks", None)
    return aligned


def modify_plan(
    plan: dict,
    modify: dict,
    search_result: dict | None = None,
    profile: dict | None = None,
    basic: dict | None = None,
) -> dict:
    """基于上一版完整计划做修改，支持全局修改（global）和 block 修改（block）。"""
    plan = without_review(plan)
    if (modify or {}).get("mode") == "block":
        return _modify_block_local(plan, modify, search_result, profile, basic)

    basic = {**(plan.get("basic") or {}), **(basic or {})}
    try:
        trip_context = (modify or {}).get("_trip_context") or resolve_trip_changes(
            (modify or {}).get("instruction") or "", basic,
            plan.get("start_date") or (search_result or {}).get("start_date") or "",
            plan.get("end_date") or (search_result or {}).get("end_date") or plan.get("start_date") or "",
        )
    except (ValueError, TypeError) as exc:
        return {"error": str(exc) or "请确认具体日期、游玩天数和预算"}
    basic = trip_context["basic"]

    kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(**kwargs)

    # 把 block_ids 转成要操作的块内容（名称/类型/天），方便 LLM 定位
    modify = dict(modify or {})
    if modify.get("mode") == "block" and modify.get("block_ids"):
        block_map = {b.get("id"): b for b in (plan.get("blocks") or [])}
        targets = []
        for bid in modify["block_ids"]:
            b = block_map.get(bid) or {}
            targets.append(
                {"name": b.get("name"), "type": b.get("type"), "day": b.get("day")}
            )
        modify["targets"] = targets

    # 传给 LLM 的 plan 只保留嵌套 plans，去掉扁平 blocks，避免结构混淆
    llm_plan = _align_modified_trip(plan, trip_context, plan)

    context: dict = {"plan": llm_plan, "modify": modify}
    if search_result:
        context["search"] = _trim_search(search_result)
    if profile:
        context["user_profile"] = profile
    if basic:
        context["basic"] = basic

    resp = _chat_completion(client,
        model=os.getenv("OPENAI_MODEL", "qwen3.8-27b"),
        messages=[
            {"role": "system", "content": MODIFY_PLAN_PROMPT +
             "输入plan中的日期、days及basic已按用户明确要求校验。必须按这些日期和天数安排每一天，"
             "不要自行更改目的地、人数或预算；增加天数时请使用检索中的真实候选安排新增日期。"
             "不得猜测或降低未改地点的价格；没有检索报价的新增地点价格留空。保留用户手选或锁定的餐厅酒店。"},
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
    result = json.loads(content)
    if not isinstance(result, dict) or not isinstance(result.get("plans"), list):
        return {"error": "调整未返回完整日程，请重试"}
    return _guard_global_plan_quotes(_align_modified_trip(result, trip_context, plan), plan,
                                    search_result or {}, modify.get("instruction") or "")


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
    used_ids = {
        str(item["id"]) for p in plan.get("plans") or []
        for day in p.get("itinerary") or []
        for item in day.get("schedule") or [] if item.get("id")
    }

    def next_id(item: dict) -> str:
        nonlocal counter
        if item.get("id"):
            return str(item["id"])
        counter += 1
        while f"b{counter}" in used_ids:
            counter += 1
        value = f"b{counter}"
        used_ids.add(value)
        return value
    for p in plan.get("plans") or []:
        style = p.get("style") or ""
        for it in p.get("itinerary") or []:
            day = it.get("day")
            date_ = it.get("date") or ""
            has_hotel_block = False
            for item in it.get("schedule") or []:
                name = item.get("name") or ""
                note = item.get("note") or ""
                typ = item.get("type") or _infer_type(name, note)
                if typ == "酒店" and any(text in str(name) for text in ("无住宿", "无需住宿", "不住宿", "当天返程", "当晚返程")):
                    continue
                if typ == "酒店":
                    has_hotel_block = True
                blocks.append(
                    {
                        **{k: v for k, v in item.items() if k not in ("_geo",)},
                        "id": next_id(item),
                        "plan_style": style,
                        "day": day,
                        "date": date_,
                        "type": typ,
                        "time": item.get("time") or "",
                        "name": name,
                        "note": note,
                        "link": item.get("link") or "",
                        "options": item.get("options") or [],
                        **({"activity_window": dict(it["activity_window"])} if it.get("activity_window") else {}),
                    }
                )
            for meal in it.get("meals") or []:
                meal_name = meal.get("meal") or ""
                options = [str(o) for o in (meal.get("options") or [])]
                blocks.append(
                    {
                        "id": next_id(meal),
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
            if hotel and not has_hotel_block and not any(text in str(hotel) for text in ("无住宿", "无需住宿", "不住宿", "当天返程", "当晚返程")):
                blocks.append(
                    {
                        "id": next_id({}),
                        "plan_style": style,
                        "day": day,
                        "date": date_,
                        "type": "酒店",
                        "time": "住宿",
                        "name": hotel,
                        "note": "推荐住宿",
                        "link": it.get("hotel_link") or "",
                    }
                )
    return blocks


def _attach_prices(plan: dict, search_result: dict, total_budget=None, basic: dict | None = None) -> dict:
    """Estimate costs per alternative, retaining actual selected prices and traveler counts."""
    def number(value):
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value.strip().lstrip("¥￥").replace(",", "")) if isinstance(value, str) else float(value)
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def name_key(value):
        # 保留分馆/分店信息，只统一括号的排版；不同馆区不能共用票价。
        return str(value or "").strip().replace("（", "(").replace("）", ")")

    source_keys = {"景点": ("poi",), "美食": ("food",), "餐饮": ("food",),
                   "酒店": ("hotels",), "活动": ("events", "promotions", "poi"),
                   "交通": ("flights", "trains")}
    sources = {}
    for key in dict.fromkeys(source for keys in source_keys.values() for source in keys):
        items = search_result.get(key) or []
        if isinstance(items, dict):
            items = [{**item, "direction": item.get("direction") or ("去" if direction == "outbound" else "回" if direction == "inbound" else "")}
                     for direction, batch in items.items() if isinstance(batch, list)
                     for item in batch if isinstance(item, dict)]
        if not isinstance(items, list):
            continue
        sources[key] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = name_key(item.get("name") or item.get("title") or (str(item.get("airline") or item.get("transport") or "") + str(item.get("flight_no") or item.get("train_no") or "")))
            price = next((number(item.get(field)) for field in ("price_per_person", "price", "ticketPrice") if number(item.get(field)) is not None), None)
            if key == "poi" and item.get("free") is True:
                price = 0
            elif price is None and key in ("events", "promotions") and item.get("free") is True:
                price = 0
            if name and price is not None:
                sources[key].append((name, price, item))

    def source_quote(block):
        name = name_key(block.get("name"))
        def compatible_transport(item):
            direction = block.get("direction")
            if direction and item.get("direction") and direction != item["direction"]:
                return False
            departure = str(block.get("dep_time") or block.get("departure_time") or "").replace(" ", "T")
            source_departure = str(item.get("dep_time") or item.get("time") or "").replace(" ", "T")
            return not (departure and source_departure and departure != source_departure)

        for source in source_keys.get(block.get("type"), ()):
            matches = [(price, item) for candidate, price, item in sources.get(source, [])
                       if candidate == name and (block.get("type") != "交通" or compatible_transport(item))]
            if not matches and block.get("type") == "交通":
                # 格式化交通名含站名，按明确班次标识匹配而非名称前缀。
                for candidate, price, item in sources.get(source, []):
                    identifier = str(item.get("flight_no") or item.get("train_no") or "")
                    if not identifier:
                        continue
                    if not re.search(r"(?<![A-Z0-9])" + re.escape(identifier) + r"(?![A-Z0-9])", name):
                        continue
                    if not compatible_transport(item):
                        continue
                    matches.append((price, item))
            if matches:
                # 同名同类多个来源若报价不同且不能核实为同一地点，保持未知。
                prices = {price for price, _ in matches}
                if len(prices) == 1:
                    return next(iter(prices))
                poi_id = block.get("poi_id")
                identified = {price for price, item in matches if poi_id and item.get("poi_id") == poi_id}
                return next(iter(identified)) if len(identified) == 1 else None
        return None
    match = re.search(r"\d+", str((basic or {}).get("travelers") or "1"))
    travelers = max(1, int(match[0])) if match else 1
    totals = {}
    missing = {}
    for block in plan.get("blocks") or []:
        explicitly_unknown = block.get("price_source") == "unknown"
        unit = None
        if not explicitly_unknown:
            if block.get("price_known") is not False:
                unit = number(block.get("unit_price"))
                if unit is None:
                    unit = number(block.get("price"))
            if unit is None:
                unit = source_quote(block)
        style = block.get("plan_style") or "推荐方案"
        basis = block.get("price_basis") if block.get("price_known") is not False and not explicitly_unknown else None
        if basis not in ("group", "per_person"):
            basis = "group" if block.get("type") == "酒店" else "per_person"
        block["unit_price"] = unit
        block["price_basis"] = basis
        block["price_known"] = unit is not None
        block["price"] = round((unit or 0) * (travelers if basis == "per_person" else 1), 2)
        totals[style] = round(totals.get(style, 0) + block["price"], 2)
        if unit is None:
            missing.setdefault(style, []).append(block.get("name"))
    budget = number(total_budget) or 0
    plan["cost_by_style"] = totals
    plan["budget_by_style"] = {style: "over" if budget and cost > budget else "unknown" if missing.get(style) else "ok" for style, cost in totals.items()}
    first_style = next(iter(totals), "")
    plan["total_cost"] = totals.get(first_style, 0)
    plan["budget_status"] = plan["budget_by_style"].get(first_style, "unknown")
    plan["unpriced_items"] = missing
    return plan


def _refresh_selected_meal_distances(plan: dict) -> None:
    """手动换店或加入候选后，距离必须对应这一餐实际相邻的景点。"""
    groups = {}
    for block in plan.get("blocks") or []:
        groups.setdefault((block.get("plan_style"), block.get("day")), []).append(block)
    for schedule in groups.values():
        for block in schedule:
            if block.get("type") not in ("美食", "餐饮") or not _valid_food_coord([block.get("lng"), block.get("lat")]):
                continue
            anchor = _meal_anchor(schedule, block.get("time") or "", {})
            if not anchor:
                for item in [block, *(block.get("options") or [])]:
                    for key in ("distance_m", "distance_km", "anchor_name", "walking_distance_m", "walking_duration_s", "walking_origin"):
                        item.pop(key, None)
                note = re.sub(r"^(?:午餐|晚餐|用餐)\s*·\s*距.*?（直线距离）[；;\s]*", "", str(block.get("note") or ""))
                block["note"] = re.sub(r"^步行约\d+米/\d+分钟[；;\s]*", "", note)
                continue
            distance = round(_km(f"{block['lng']},{block['lat']}", f"{anchor['longitude']},{anchor['latitude']}") * 1000)
            origin = _coord_key([anchor["longitude"], anchor["latitude"]])
            dest = _coord_key([block["lng"], block["lat"]])
            route = walking_route(origin, dest, cached_only=True)
            # 改景点/时间后，旧锚点的步行距离不能继续展示。
            block.update(walking_distance_m=None, walking_duration_s=None)
            block.update(distance_m=distance, anchor_name=anchor["anchor_name"], walking_origin=origin)
            if route:
                block.update(walking_distance_m=route["distance_m"], walking_duration_s=route["duration_s"])
            note = re.sub(r"^(?:午餐|晚餐|用餐)\s*·\s*距.*?（直线距离）[；;\s]*", "", str(block.get("note") or ""))
            note = re.sub(r"^步行约\d+米/\d+分钟[；;\s]*", "", note)
            block["note"] = _meal_distance_note(block.get("meal") or "用餐", anchor["anchor_name"], block) + (f"；{note}" if note else "")
            for option in block.get("options") or []:
                if _valid_food_coord([option.get("lng"), option.get("lat")]):
                    option["distance_m"] = round(_km(f"{option['lng']},{option['lat']}", f"{anchor['longitude']},{anchor['latitude']}") * 1000)
                    option["distance_km"] = round(option["distance_m"] / 1000, 1)
                    cached_route = walking_route(origin, _coord_key([option["lng"], option["lat"]]), cached_only=True)
                    option.update(walking_distance_m=None, walking_duration_s=None)
                    option["walking_origin"] = origin
                    if cached_route:
                        option.update(walking_distance_m=cached_route["distance_m"], walking_duration_s=cached_route["duration_s"])


def _align_route_times(plan: dict, adjust: bool = False) -> None:
    """初次生成按真实交通时间顺延；局部修改保留时间并提示冲突。"""
    by_id = {block.get("id"): block for block in plan.get("blocks") or []}
    warnings = []
    for leg in plan.get("legs") or []:
        origin, target = by_id.get(leg.get("from")), by_id.get(leg.get("to"))
        if not origin or not target or origin.get("day") != target.get("day"):
            continue
        if origin.get("type") in ("酒店", "交通") or target.get("type") in ("酒店", "交通"):
            continue
        before, after = _schedule_range(origin), _schedule_range(target)
        duration = _positive_food_number(leg.get("duration_s"))
        if not before or not after or duration is None:
            continue
        transfer = math.ceil(duration / 60) + 5
        earliest = before[1] + transfer
        if after[0] >= earliest:
            continue
        latest = int((target.get("activity_window") or {}).get("end_min") or 24 * 60)
        if target.get("type") in ("美食", "餐饮"):
            meal = target.get("meal") or ""
            latest = min(latest, 14 * 60 if meal == "午餐" else 20 * 60 if meal == "晚餐" else latest)
        length = after[1] - after[0]
        if adjust and not (target.get("user_selected") or target.get("locked")) and earliest + length <= latest:
            target["time"] = f"{_clock(earliest)}-{_clock(earliest + length)}"
            target["note"] = (target.get("note") or "") + f"；已预留约{transfer}分钟转场"
        else:
            warnings.append(f"第{target.get('day')}天从{origin.get('name')}到{target.get('name')}需约{transfer}分钟转场，当前时间不足，请调整用餐或游览时间。")
    old_warnings = set(plan.get("travel_time_warnings") or [])
    plan["warnings"] = [warning for warning in plan.get("warnings") or [] if warning not in old_warnings] + warnings
    plan["travel_time_warnings"] = warnings
    # 右侧候选保留实际餐点时间，不能沿用顺延前的旧时间。
    meals = {(block.get("plan_style"), block.get("day"), block.get("meal")): block.get("time")
        for block in by_id.values() if block.get("type") in ("美食", "餐饮") and block.get("meal")}
    for entry in [*(plan.get("food") or []), *(plan.get("food_by_anchor") or [])]:
        time = meals.get((entry.get("plan_style"), entry.get("day"), entry.get("meal")))
        if time:
            entry["time"] = time


def finalize_plan(plan: dict, search_result: dict | None = None, basic: dict | None = None,
                  refresh_food: bool = False, refresh_food_targets: list | None = None) -> dict:
    """Rebuild itineraries, meal anchors, prices and map after a mutation, without another LLM call."""
    plan = without_review(plan)
    result = rebuild_itineraries(plan) if isinstance(plan.get("blocks"), list) else dict(plan)
    search_result = dict(search_result or {})
    if refresh_food:
        result = refresh_food_for_plan(result, search_result, basic, force=True, targets=refresh_food_targets)
        result["blocks"] = blockify(result)
    elif not isinstance(result.get("blocks"), list):
        result["blocks"] = blockify(result)
    if result.get("food"):
        search_result["food"] = result["food"]
    result["legs"] = []
    # 兼容历史已保存的数字索引；新计划用实际餐厅名表示用户选择。
    for block in result.get("blocks") or []:
        selected = block.get("selected_option")
        options = block.get("options") or []
        if isinstance(selected, int) and 0 <= selected < len(options):
            block["selected_option"] = options[selected].get("name")
    _attach_prices(result, search_result, (basic or {}).get("total_budget"), basic)
    attach_routes(result, result.get("destination") or search_result.get("destination") or "")
    _align_route_times(result)
    _refresh_selected_meal_distances(result)
    result["basic"] = dict(basic or {})
    return rebuild_itineraries(result)


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
        if isinstance(data, dict) and data.get("finalize"):
            result = finalize_plan(
                data.get("plan") or {}, data.get("search"), data.get("basic"),
                bool(data.get("refresh_food")), data.get("refresh_food_targets"),
            )
            print(json.dumps(result, ensure_ascii=False))
            return
        if isinstance(data, dict) and data.get("classify"):
            kwargs: dict = {"api_key": os.getenv("OPENAI_API_KEY")}
            if os.getenv("OPENAI_BASE_URL"):
                kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
            client = OpenAI(**kwargs)
            result = classify_modify(
                client,
                data.get("instruction") or "",
                data.get("blocks") or [],
            )
            print(json.dumps(result, ensure_ascii=False))
            return

        # 完整计划修改：输入含 plan + modify（支持全局修改 / block 修改）
        if isinstance(data, dict) and data.get("plan") is not None and data.get("modify"):
            result = modify_plan(
                data.get("plan") or {},
                data.get("modify") or {},
                data.get("search"),
                data.get("profile"),
                data.get("basic"),
            )
            if isinstance(result, dict) and "error" not in result:
                result["blocks"] = blockify(result)
                if not data.get("defer_finalize"):
                    updated_basic = result.get("basic") if isinstance(result.get("basic"), dict) else data.get("basic")
                    result = finalize_plan(result, data.get("search"), updated_basic, refresh_food=True,
                                           refresh_food_targets=data.get("refresh_food_targets"))
        # 局部修改模式：输入含 blocks + instruction，只改选中块
        elif isinstance(data, dict) and data.get("blocks") is not None and data.get("instruction"):
            result = modify_blocks(
                data.get("blocks") or [],
                data.get("instruction") or "",
                data.get("search"),
                data.get("profile"),
                data.get("basic"),
                data.get("full_blocks"),
            )
            # 改过的块重新定位，前端据此重画路线
            if isinstance(result, dict) and isinstance(result.get("blocks"), list):
                geocode_blocks(result["blocks"], (data.get("search") or {}).get("destination") or "")
                strip_geo(result["blocks"])
        else:
            if isinstance(data, dict) and any(
                k in data
                for k in (
                    "search",
                    "profile",
                    "preferences",
                    "recent_trips",
                    "recent_trip_summary",
                    "skip_trip_summary",
                    "basic",
                    "feedback",
                    "modify",
                )
            ):
                search_result = data.get("search") or {}
                profile = data.get("profile")
                preferences = data.get("preferences")
                recent_trips = data.get("recent_trips")
                recent_trip_summary = data.get("recent_trip_summary")
                skip_trip_summary = bool(data.get("skip_trip_summary"))
                basic = data.get("basic")
                feedback = data.get("feedback")
                modify = data.get("modify")
            else:
                search_result = data
                profile = None
                preferences = None
                recent_trips = None
                recent_trip_summary = None
                skip_trip_summary = False
                basic = None
                feedback = None
                modify = None
            result = build_plan(
                search_result,
                profile,
                preferences,
                recent_trips,
                basic,
                feedback,
                modify,
                recent_trip_summary,
                skip_trip_summary,
            )
            if isinstance(result, dict) and "error" not in result:
                result["blocks"] = blockify(result)
                search_result = merge_plan_sources(search_result, result)
                result = _attach_prices(
                    result, search_result, (basic or {}).get("total_budget"), basic
                )
                attach_routes(result, result.get("destination") or "")
                _align_route_times(result, adjust=True)
                _refresh_selected_meal_distances(result)
                result = rebuild_itineraries(result)
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
