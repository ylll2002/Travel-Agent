"""SearchAgent 的工具服务（MCP server）与确定性搜索编排。

结构：
- 结构化核心函数 ``_fetch_*``：返回 dict / list[dict]，供 JSON 输出与编排使用
- 文本格式化函数 ``_format_*``：把结构化数据转成可读文本
- MCP 工具：把结构化数据格式化成文本，供 LLM agent 调用
- ``run_search``：确定性综合编排，输入 JSON 输出 JSON（不经过 LLM）
"""

import json
import hashlib
import math
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import certifi
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from openai import OpenAI
from tavily import TavilyClient

from amap_service import search_restaurants

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
from agent_env import subprocess_env  # noqa: E402

load_dotenv(BASE_DIR / ".env")

CACHE_DIR = BASE_DIR / "cache"
CACHE_TTL = 3600  # 缓存有效期（秒），1 小时
SEARCH_VERSION = "hotel30-v10-parallel-profile-food"  # 搜索逻辑版本，变更后自动让旧缓存失效
FLYAI_TIMEOUT = 30  # 单个来源超时后继续使用其他候选，避免整条规划等待数分钟

server = FastMCP(
    "search-tools",
    instructions="SearchAgent 的工具集",
    log_level="ERROR",
)

_tavily_client: TavilyClient | None = None


def _get_tavily() -> TavilyClient:
    """懒加载 Tavily 客户端，避免 import 阶段就要求 API key。"""
    global _tavily_client
    if _tavily_client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("缺少 TAVILY_API_KEY，请在 SearchAgent/.env 中配置")
        _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client


POI_FEATURE_PROMPT = (
    "你是景点特征提取助手。为每个景点提取 3~6 个简短特征标签（逗号分隔，每个 2~4 字）。"
    "标签应覆盖：类型与属性（文化/历史/自然/亲子/宗教/演出/购物/美食/运动/科技/湖景/园林等）、"
    "环境（室内/户外）、适合人群（亲子/情侣/老人/学生等）。"
    "同时估计每个景点的建议游玩时长（可选值：0.5-1小时、1-2小时、2-3小时、半天、全天）"
    "和景点规模（大/中/小，大=大型景区或需要大量步行，小=单点或小展馆）。"
    "输出 JSON：{\"features\":[\"文化,历史,室内\",...],\"durations\":[\"2-3小时\",...],\"scales\":[\"中\",...]}，"
    "三个数组顺序都与输入景点一致。"
    "只输出 JSON，不要任何多余文字或代码块。"
)

EVENTS_FEATURE_PROMPT = (
    "你是活动信息提炼助手。为每个活动提炼 3~6 个特征标签（逗号分隔，每个 2~4 字）。"
    "标签覆盖：活动类型（演唱会/音乐节/比赛/展览/节日/体育等）、室内外、适合人群、时间季节。"
    "输出 JSON：{\"features\":[\"音乐节,户外,10月\",...]}，顺序与输入一致。"
    "只输出 JSON，不要任何多余文字或代码块。"
)

EVENT_EXTRACT_PROMPT = (
    "你是活动信息提取助手。从网页搜索结果中提取具体的活动。"
    "输出 JSON：{\"events\":[{\"name\":\"具体活动名称，如「某某演唱会」\","
    "\"type\":\"演唱会/音乐节/比赛/展览/节日等\",\"date\":\"活动日期或时间\","
    "\"url\":\"对应活动链接\"}]}。"
    "只保留具体活动，去掉「活动汇总」「榜单」「门户首页」等非具体活动；"
    "如果某条结果本身就是具体活动，保留它的标题作为 name。"
    "最多返回 20 个，只输出 JSON，不要任何多余文字。"
)

FOOD_FEATURE_PROMPT = (
    "你是美食信息提炼助手。为每个美食提炼 3~6 个特征标签（逗号分隔，每个 2~4 字）。"
    "标签覆盖：菜系、特色菜品、价位、环境、适合人群。"
    "输出 JSON：{\"features\":[\"杭帮菜,人均100,必吃\",...]}，顺序与输入一致。"
    "只输出 JSON，不要任何多余文字或代码块。"
)

RESTAURANT_PROMPT = (
    "你是美食餐厅提取助手。根据多个社交平台的搜索结果，提取当地美食的候选餐厅。"
    "输出 JSON：{\"food\":[{\"category\":\"菜系或类型\",\"options\":["
    "{\"name\":\"餐厅名\",\"link\":\"平台链接\",\"source\":\"抖音/大众点评/小红书\"},"
    "{\"name\":\"餐厅名2\",\"link\":\"链接2\",\"source\":\"平台\"}]}]}。"
    "每个 category 返回两个候选餐厅（options），链接优先来自抖音/大众点评/小红书的具体内容链接，"
    "找不到对应平台链接就留空字符串。提取 4~5 个 category，共 8~10 个餐厅。"
    "餐厅名和链接尽量来自搜索结果原文，不要编造。只输出 JSON，不要任何多余文字或代码块。"
)


def _flyai_bin() -> Path:
    """定位 npm 安装的 flyai CLI 入口。

    npm 为同一个 bin 生成的启动器在不同平台不一样：

    * macOS / Linux：无扩展名的可执行 shell 脚本（带 shebang，内核可直接执行）
    * Windows：``flyai.cmd``（供 cmd.exe）与 ``flyai.ps1``（供 PowerShell）

    Windows 上虽然也存在无扩展名的 ``flyai``，但它是 ``#!/bin/sh`` 脚本，
    直接执行会报 ``OSError: [WinError 193] 不是有效的 Win32 应用程序``。
    因此必须按平台选择可执行的启动器。
    """
    bin_dir = Path(__file__).resolve().parent / "node_modules" / ".bin"
    if os.name == "nt":
        candidates = (bin_dir / "flyai.cmd", bin_dir / "flyai.exe", bin_dir / "flyai")
    else:
        candidates = (bin_dir / "flyai", bin_dir / "flyai.cmd")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


FLYAI_BIN = _flyai_bin()

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

WEATHER_CODES = {
    0: "晴朗",
    1: "基本晴朗",
    2: "局部多云",
    3: "阴天",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "阵雨",
    81: "中阵雨",
    82: "强阵雨",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


def _http_get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "SearchAgent/0.1"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _run_flyai(args: list[str]) -> dict:
    """调用 flyai CLI 并解析其 JSON 输出。"""
    if not FLYAI_BIN.is_file():
        raise RuntimeError(
            f"未找到 flyai CLI：{FLYAI_BIN}\n"
            "请在 SearchAgent 目录执行 `npm install` 安装 Node 依赖"
            "（依赖声明于 SearchAgent/package.json）。"
        )
    cmd = [str(FLYAI_BIN), *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FLYAI_TIMEOUT,
        env=subprocess_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"flyai 调用失败：{detail}")
    return json.loads(result.stdout.strip())


def _normalize_date(value: str) -> str:
    """把各种日期格式统一成 YYYY-MM-DD。"""
    value = value.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", value)
    if m:
        return f"{date.today().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})[日号]?", value)
    if m:
        return f"{date.today().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return value


def _geocode_city(city: str) -> tuple[str, str, str, str]:
    """地理编码：返回 (纬度, 经度, 名称, 国家)。"""
    query = urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"}
    )
    geo = _http_get_json(f"{GEOCODING_URL}?{query}")
    results = geo.get("results") or []
    if not results:
        raise ValueError(f"未找到城市「{city}」，请换个写法试试。")
    place = results[0]
    return (
        str(place["latitude"]),
        str(place["longitude"]),
        place.get("name", city),
        place.get("country", ""),
    )


# ================= 结构化核心（返回 dict / list[dict]） =================


def _fetch_weather(city: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    latitude, longitude, name, country = _geocode_city(city)
    today = date.today()
    try:
        start = date.fromisoformat(_normalize_date(start_date)) if start_date else today
        end = date.fromisoformat(_normalize_date(end_date)) if end_date else start
    except ValueError as exc:
        raise ValueError(f"日期无法识别：{start_date or end_date}") from exc

    base_url = ARCHIVE_URL if end < today else FORECAST_URL
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,relative_humidity_2m_mean",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "auto",
    }
    data = _http_get_json(f"{base_url}?{urllib.parse.urlencode(params)}")
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    days = []
    for i, day in enumerate(times):
        code = daily.get("weather_code", [])[i]
        days.append(
            {
                "date": day,
                "weather": WEATHER_CODES.get(code, f"代码{code}"),
                "weather_code": code,
                "temp_min": daily.get("temperature_2m_min", [])[i],
                "temp_max": daily.get("temperature_2m_max", [])[i],
                "humidity": daily.get("relative_humidity_2m_mean", [])[i],
            }
        )
    return {
        "location": name,
        "country": country,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": days,
    }


def _fetch_hotels_once(
    destination: str,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    max_price: float | None = None,
    hotel_stars: str | None = None,
    hotel_types: str | None = None,
    key_words: str | None = None,
    sort: str | None = None,
) -> list[dict]:
    args = ["search-hotel", "--dest-name", destination]
    if check_in_date:
        args += ["--check-in-date", _normalize_date(check_in_date)]
    if check_out_date:
        args += ["--check-out-date", _normalize_date(check_out_date)]
    if max_price is not None:
        args += ["--max-price", str(int(max_price))]
    if hotel_stars:
        args += ["--hotel-stars", hotel_stars]
    if hotel_types:
        args += ["--hotel-types", hotel_types]
    if key_words:
        args += ["--key-words", key_words]
    if sort:
        args += ["--sort", sort]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    return [
        {
            "name": item.get("name") or "",
            "star": item.get("star") or "",
            "score": item.get("scoreDesc") or item.get("score") or "",
            "price": item.get("price") or "",
            "location": item.get("interestsPoi") or item.get("address") or "",
            "url": item.get("detailUrl") or "",
            "longitude": item.get("longitude"),
            "latitude": item.get("latitude"),
            "image": (
                item.get("mainPic")
                or item.get("picUrl")
                or item.get("pictureUrl")
                or item.get("imgUrl")
                or item.get("imageUrl")
                or item.get("mainImage")
                or ""
            ),
        }
        for item in items
    ]


def _fetch_hotels(
    destination: str,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    max_price: float | None = None,
    hotel_stars: str | None = None,
    hotel_types: str | None = None,
    key_words: str | None = None,
    sort: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """搜索酒店：飞猪无分页，用多种排序多次搜索后去重，尽量凑到 limit 条。"""
    if sort:
        return _fetch_hotels_once(
            destination,
            check_in_date,
            check_out_date,
            max_price,
            hotel_stars,
            hotel_types,
            key_words,
            sort,
        )[:limit]

    seen: set[str] = set()
    result: list[dict] = []
    rounds = [
        {"sort": "rate_desc"},
        {"sort": "rate_desc", "hotel_stars": "5"},
        {"sort": "rate_desc", "hotel_stars": "4"},
        {"sort": "price_asc", "hotel_stars": "3"},
        {"sort": "no_rank", "key_words": "如家 汉庭 全季 亚朵 维也纳"},
    ]
    def fetch_round(params: dict) -> list[dict]:
        try:
            return _fetch_hotels_once(
                destination,
                check_in_date,
                check_out_date,
                max_price,
                params.get("hotel_stars") or hotel_stars,
                hotel_types,
                params.get("key_words") or key_words,
                params.get("sort") or sort,
            )
        except Exception:
            return []

    # 各排序互不依赖；并行取回后按原排序顺序合并，仍优先高评分酒店。
    with ThreadPoolExecutor(max_workers=3) as executor:
        batches = list(executor.map(fetch_round, rounds))
    for items in batches:
        for item in items:
            name = item.get("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(item)
            if len(result) >= limit:
                break
        if len(result) >= limit:
            break
    return result


def _fetch_flights(
    origin: str,
    destination: str | None = None,
    dep_date: str | None = None,
    back_date: str | None = None,
    journey_type: str | None = None,
    sort_type: str | None = None,
    max_price: float | None = None,
) -> list[dict]:
    args = ["search-flight", "--origin", origin]
    if destination:
        args += ["--destination", destination]
    if dep_date:
        args += ["--dep-date", _normalize_date(dep_date)]
    if back_date:
        args += ["--back-date", _normalize_date(back_date)]
    if journey_type:
        args += ["--journey-type", journey_type]
    if sort_type:
        args += ["--sort-type", sort_type]
    if max_price is not None:
        args += ["--max-price", str(int(max_price))]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    result = []
    for item in items:
        journeys = item.get("journeys") or []
        segment = (journeys[0].get("segments") or [{}])[0] if journeys else {}
        arr_city = segment.get("arrCityName") or ""
        if destination and arr_city and destination not in arr_city:
            continue
        result.append(
            {
                "airline": segment.get("marketingTransportName") or "",
                "flight_no": segment.get("marketingTransportNo") or "",
                "dep_station": segment.get("depStationName") or "",
                "arr_station": segment.get("arrStationName") or "",
                "dep_time": segment.get("depDateTime") or "",
                "arr_time": segment.get("arrDateTime") or "",
                "seat": segment.get("seatClassName") or "",
                "duration": item.get("totalDuration") or "",
                "price": item.get("ticketPrice") or item.get("adultPrice") or "",
                "url": item.get("jumpUrl") or "",
            }
        )
    return result


def _fetch_trains(
    origin: str,
    destination: str | None = None,
    dep_date: str | None = None,
    journey_type: str | None = None,
    sort_type: str | None = None,
) -> list[dict]:
    """搜索高铁/火车票（飞猪 search-train）。"""
    args = ["search-train", "--origin", origin]
    if destination:
        args += ["--destination", destination]
    if dep_date:
        args += ["--dep-date", _normalize_date(dep_date)]
    if journey_type:
        args += ["--journey-type", journey_type]
    if sort_type:
        args += ["--sort-type", sort_type]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    result = []
    for item in items:
        journeys = item.get("journeys") or []
        segment = (journeys[0].get("segments") or [{}])[0] if journeys else {}
        arr_city = segment.get("arrCityName") or ""
        if destination and arr_city and destination not in arr_city:
            continue
        result.append(
            {
                "transport": segment.get("marketingTransportName") or "火车",
                "train_no": segment.get("marketingTransportNo") or "",
                "dep_station": segment.get("depStationName") or "",
                "arr_station": segment.get("arrStationName") or "",
                "dep_time": segment.get("depDateTime") or "",
                "arr_time": segment.get("arrDateTime") or "",
                "seat": segment.get("seatClassName") or "",
                "duration": item.get("totalDuration") or "",
                "price": item.get("price") or "",
                "url": item.get("jumpUrl") or "",
            }
        )
    return result


def _fetch_round_trip(
    fetcher,
    origin: str,
    destination: str,
    start: str,
    end: str,
) -> list[dict]:
    """查去程（origin→destination，start 出发）+ 回程（反向，end 出发），每条带 direction。"""
    with ThreadPoolExecutor(max_workers=2) as executor:
        outbound_future = executor.submit(fetcher, origin, destination, start, journey_type="1")
        inbound_future = executor.submit(fetcher, destination, origin, end, journey_type="1")
        # 一个方向不可用不应该丢掉另一方向已找到的车次/航班。
        try:
            outbound = outbound_future.result()
        except Exception:
            outbound = []
        try:
            inbound = inbound_future.result()
        except Exception:
            inbound = []
    for x in outbound:
        x["direction"] = "去"
    for x in inbound:
        x["direction"] = "回"
    return outbound + inbound


def _feature_model_options() -> dict:
    """特征摘要不需要长思考；失败立即用来源文本，交给规划模型继续。"""
    model = os.getenv("OPENAI_MODEL", "").lower()
    if "qwen" in model or "deepseek" in model:
        return {"extra_body": {"enable_thinking": False}}
    return {}


def _extract_poi_features(pois: list[dict]) -> list[dict]:
    """用 LLM 为每个景点提取简短特征标签，替换长 description；失败则回退截断。"""
    if not pois:
        return pois
    try:
        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            max_retries=0,
        )
        brief = [
            {
                "name": p.get("name") or "",
                "category": p.get("category") or "",
                "description": (p.get("description") or "")[:200],
            }
            for p in pois
        ]
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash"),
            messages=[
                {"role": "system", "content": POI_FEATURE_PROMPT},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=3500,
            timeout=20,
            **_feature_model_options(),
        )
        content = (resp.choices[0].message.content or "{}").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        features = json.loads(content).get("features") or []
        durations = json.loads(content).get("durations") or []
        scales = json.loads(content).get("scales") or []
        for i, p in enumerate(pois):
            p["description"] = features[i] if i < len(features) else ""
            p["duration"] = durations[i] if i < len(durations) else ""
            p["scale"] = scales[i] if i < len(scales) else ""
        return pois
    except Exception:
        for p in pois:
            p["description"] = (p.get("description") or "")[:80]
            p.setdefault("duration", "")
            p.setdefault("scale", "")
        return pois


def _extract_item_features(items: list[dict], prompt: str) -> list[dict]:
    """用 LLM 为活动/美食提炼特征标签，替换长 content；失败则回退截断。"""
    if not items:
        return items
    try:
        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            max_retries=0,
        )
        brief = [
            {
                "title": x.get("title") or "",
                "content": (x.get("content") or "")[:200],
            }
            for x in items
        ]
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash"),
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
            timeout=20,
            **_feature_model_options(),
        )
        content = (resp.choices[0].message.content or "{}").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        features = json.loads(content).get("features") or []
        for i, x in enumerate(items):
            x["content"] = features[i] if i < len(features) else ""
        return items
    except Exception:
        for x in items:
            x["content"] = (x.get("content") or "")[:80]
        return items


def _pick_district(item: dict) -> str:
    for key in ("districtName", "district", "areaName", "region", "address"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _extract_district_label(value: str) -> str:
    """从地址里抽出区县级行政区，如「西湖区」「淳安县」。"""
    value = (value or "").strip()
    if not value:
        return ""
    candidates: list[tuple[int, str]] = []
    for candidate in ("自治县", "区", "县", "旗"):
        start = 0
        while True:
            idx = value.find(candidate, start)
            if idx == -1:
                break
            # 跳过「自治区」里的「区」
            if candidate == "区" and idx >= 2 and value[idx - 2:idx] == "自治":
                start = idx + 1
                continue
            candidates.append((idx, candidate))
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


def _poi_hot_key(item: dict):
    match = re.search(r"第\s*(\d+)\s*名", str(item.get("rank") or ""))
    if not match:
        return (1, 0)
    return (0, int(match.group(1)))


def _matches_requested_poi(requested: str, candidate: str) -> bool:
    """判断候选景点是否就是用户点名的景点；容忍景区常用后缀，避免“西湖天地”冒充“西湖”。"""
    _place_suffix = re.compile(
        r"(?:风景名胜区|风景区|景区|博物院|博物馆|纪念馆|展览馆|馆区|分馆|公园|乐园|植物园|动物园|海洋馆)$"
    )

    def normalize(value: str) -> str:
        value = re.sub(r"[（(].*?[）)]", "", str(value)).strip()
        return _place_suffix.sub("", value)

    expected, actual = normalize(requested), normalize(candidate)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    # 较长名称之间允许包含关系（如“故宫博物院”与“故宫”），短名不参与包含判断。
    if len(expected) >= 4 and len(actual) >= 4 and (expected in actual or actual in expected):
        return True
    return False


def _exact_poi_match(requested: str, candidate: str) -> bool:
    """严格匹配：规范化后完全相等，避免把「武汉大学樱花大道」当成「武汉大学」。"""
    _place_suffix = re.compile(
        r"(?:风景名胜区|风景区|景区|博物院|博物馆|纪念馆|展览馆|馆区|分馆|公园|乐园|植物园|动物园|海洋馆)$"
    )

    def normalize(value: str) -> str:
        value = re.sub(r"[（(].*?[）)]", "", str(value)).strip()
        return _place_suffix.sub("", value)

    expected = normalize(requested)
    return bool(expected) and expected == normalize(candidate)


POI_CATEGORY_GROUPS = {
    "自然景观": ["自然风光", "山湖田园", "森林丛林", "峡谷瀑布", "沙滩海岛", "沙漠草原"],
    "历史人文": ["人文古迹", "古镇古村", "历史古迹", "园林花园", "宗教场所", "博物馆", "纪念馆", "展览馆"],
    "主题娱乐": ["公园乐园", "主题乐园", "水上乐园", "影视基地", "动物园", "植物园", "海洋馆", "体育场馆", "演出赛事", "剧院剧场", "温泉"],
    "城市地标与购物": ["地标建筑", "市集", "文创街区", "城市观光"],
    "户外运动与体验": ["户外活动", "滑雪", "漂流", "冲浪", "潜水", "露营"],
}


_CHINESE_CATEGORY_LABEL = {
    "自然风光": "自然景观", "山湖田园": "自然景观", "森林丛林": "自然景观",
    "峡谷瀑布": "自然景观", "沙滩海岛": "自然景观", "沙漠草原": "自然景观",
    "人文古迹": "历史人文", "古镇古村": "历史人文", "历史古迹": "历史人文",
    "园林花园": "历史人文", "宗教场所": "历史人文", "博物馆": "历史人文",
    "纪念馆": "历史人文", "展览馆": "历史人文",
    "公园乐园": "主题娱乐", "主题乐园": "主题娱乐", "水上乐园": "主题娱乐",
    "影视基地": "主题娱乐", "动物园": "主题娱乐", "植物园": "主题娱乐",
    "海洋馆": "主题娱乐", "体育场馆": "主题娱乐", "演出赛事": "主题娱乐",
    "剧院剧场": "主题娱乐", "温泉": "主题娱乐",
    "地标建筑": "城市地标与购物", "市集": "城市地标与购物",
    "文创街区": "城市地标与购物", "城市观光": "城市地标与购物",
    "户外活动": "户外运动与体验", "滑雪": "户外运动与体验", "漂流": "户外运动与体验",
    "冲浪": "户外运动与体验", "潜水": "户外运动与体验", "露营": "户外运动与体验",
}


def _category_label(item: dict) -> str:
    cat = str(item.get("category") or "").strip()
    return _CHINESE_CATEGORY_LABEL.get(cat, "")


def _search_poi_items(
    city_name: str,
    keyword: str | None = None,
    category: str | None = None,
    poi_level: str | None = None,
) -> list[dict]:
    args = ["search-poi", "--city-name", city_name]
    if keyword:
        args += ["--keyword", keyword]
    if category:
        args += ["--category", category]
    if poi_level:
        args += ["--poi-level", str(poi_level)]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    pois = []
    for item in items:
        raw_district = _pick_district(item)
        address = item.get("address") or ""
        pois.append(
            {
            "name": item.get("name") or "",
            "category": item.get("category") or "",
            "rank": item.get("listRank") or "",
            "free": item.get("freePoiStatus") == "FREE",
            "description": item.get("description") or "",
            "url": item.get("jumpUrl") or "",
            "longitude": item.get("longitude"),
            "latitude": item.get("latitude"),
            "image": (
                item.get("mainPic")
                or item.get("picUrl")
                or item.get("pictureUrl")
                or item.get("imgUrl")
                or item.get("imageUrl")
                or item.get("mainImage")
                or ""
            ),
            "district": raw_district,
            "district_label": _extract_district_label(raw_district)
            or _extract_district_label(address),
        }
        )
    return pois


def _fetch_poi(
    city_name: str,
    keyword: str | None = None,
    category: str | None = None,
    poi_level: str | None = None,
) -> list[dict]:
    pois = _search_poi_items(city_name, keyword, category, poi_level)
    return _extract_poi_features(pois)


def _fetch_poi_distributed(
    city_name: str,
    target: int = 50,
    travel_styles: list[str] | None = None,
) -> list[dict]:
    """榜单前十 + 五个大类各取若干条，去重后按热门度返回。"""
    travel_styles = set(travel_styles or [])

    # 1. 榜单前十（经典必去）
    try:
        top = _search_poi_items(city_name)
    except Exception:
        top = []
    for item in top:
        item["category_label"] = _category_label(item)

    # 2. 五个大类各查英文 category，匹配的 travel_style 多取 3 条
    by_label: dict[str, list[dict]] = {}
    for label, cats in POI_CATEGORY_GROUPS.items():
        limit = 13 if label in travel_styles else 10
        collected: list[dict] = []
        for cat in cats:
            if len(collected) >= limit:
                break
            try:
                for item in _search_poi_items(city_name, category=cat):
                    if len(collected) >= limit:
                        break
                    item["category_label"] = label
                    collected.append(item)
            except Exception:
                pass
        by_label[label] = collected

    # 3. 去重：榜单前十优先，再按大类顺序补齐
    seen: set[str] = set()
    ordered: list[dict] = []
    for item in top:
        name = (item.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(item)
    for label in POI_CATEGORY_GROUPS:
        for item in by_label[label]:
            name = (item.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                ordered.append(item)

    ordered.sort(key=_poi_hot_key)
    return _extract_poi_features(ordered[:target])


def _fetch_promotions(keyword: str | None = None) -> list[dict]:
    """检索飞猪促销活动/优惠商品（特价机票卡、券包、酒店套餐等）。"""
    query = (keyword or "").strip() or "促销活动 特价 优惠"
    data = _run_flyai(["keyword-search", "--query", query])
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    result = []
    for item in items:
        info = item.get("info") or {}
        result.append(
            {
                "title": info.get("title") or "",
                "price": info.get("price") or "",
                "star": info.get("star") or "",
                "tags": info.get("tags") or "",
                "image": info.get("picUrl") or "",
                "url": info.get("jumpUrl") or "",
            }
        )
    return result


def _fetch_web_search(
    query: str,
    max_results: int = 10,
    search_depth: str = "advanced",
) -> list[dict]:
    """通用网页搜索（Tavily），返回 LLM 优化的结果。"""
    client = _get_tavily()
    resp = client.search(
        query=query,
        max_results=max_results,
        search_depth=search_depth,
        timeout=20,
    )
    return [
        {
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "content": (r.get("content") or "")[:600],
            "score": r.get("score"),
        }
        for r in (resp.get("results") or [])
    ]


def _extract_events(items: list[dict]) -> list[dict]:
    """从 Tavily 网页结果中提取具体活动名称、类型、日期和链接。"""
    if not items:
        return []
    try:
        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            max_retries=0,
        )
        brief = [
            {"title": x.get("title") or "", "url": x.get("url") or "", "content": (x.get("content") or "")[:300]}
            for x in items
        ]
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "deepseek-v4.1-flash"),
            messages=[
                {"role": "system", "content": EVENT_EXTRACT_PROMPT},
                {"role": "user", "content": json.dumps({"results": brief}, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=1800,
            timeout=20,
            **_feature_model_options(),
        )
        content = (resp.choices[0].message.content or "{}").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        events = json.loads(content).get("events") or []
        result = []
        for e in events:
            name = (e.get("name") or "").strip()
            if not name:
                continue
            typ = (e.get("type") or "").strip()
            date_ = (e.get("date") or "").strip()
            result.append(
                {
                    "title": name,
                    "content": f"{typ},{date_}" if typ or date_ else "",
                    "url": e.get("url") or "",
                }
            )
        if result:
            return result
        # LLM 没提取出活动时，回退到原始网页结果，避免前端活动一栏为空
        return [
            {
                "title": x.get("title") or "",
                "content": (x.get("content") or "")[:120],
                "url": x.get("url") or "",
            }
            for x in items
        ]
    except Exception:
        return [
            {
                "title": x.get("title") or "",
                "content": (x.get("content") or "")[:120],
                "url": x.get("url") or "",
            }
            for x in items
        ]


def _fetch_events(
    destination: str,
    start_date: str | None = None,
    end_date: str | None = None,
    max_results: int = 20,
) -> list[dict]:
    """搜索某地在日期段内的热点活动（演唱会/比赛/节日等），复用 Tavily。"""
    start = _normalize_date(start_date) if start_date else ""
    end = _normalize_date(end_date) if end_date else ""
    if start and end:
        date_range = f"{start}到{end}"
    elif start:
        date_range = start
    else:
        date_range = "近期"
    query = f"{destination} {date_range} 演唱会 音乐节 比赛 展览 节日 活动 热点"
    for _ in range(2):
        try:
            items = _fetch_web_search(query, max_results=max_results)
            events = _extract_events(items)
            if events:
                return events
        except Exception:
            continue
    return []


def _fetch_food(
    destination: str,
    keyword: str | None = None,
    max_price: float | None = None,
    max_results: int = 50,
    location: str | None = None,
    radius: int = 1500,
    allow_web_fallback: bool = True,
) -> list[dict]:
    """用高德地图 POI 搜索当地餐厅，返回结构化数据。

    优先调用高德（返回 name/address/lng/lat/cuisine/rating/人均/商圈/地图链接/POI详情链接）；
    若未配置 AMAP_KEY 或调用失败，回退到 Tavily 网页搜索，并在结果中标记来源。

    每条结果带 _source 字段："amap" 或 "tavily"，便于下游区分数据格式。
    """
    # 1. 优先高德
    try:
        restaurants = search_restaurants(
            destination,
            keyword=keyword,
            limit=max_results,
            location=location,
            radius=radius,
        )
        if restaurants:
            items = [r.to_dict() for r in restaurants]
            # 按人均预算过滤（如果有）
            if not location and max_price is not None and max_price > 0:
                items = [
                    it for it in items
                    if it.get("price_per_person") is None or it["price_per_person"] <= max_price
                ]
            for it in items:
                it["_source"] = "amap"
            return items
        # 高德返回空也算失败，走回退
        raise RuntimeError("高德返回 0 条餐厅结果")
    except Exception as exc:  # noqa: BLE001
        amap_error = str(exc)
        # 网页结果没有可信的坐标，不能作为周边餐厅塞进地图路线。
        if location or not allow_web_fallback:
            print(f"[food] 周边餐厅暂不可用：{type(exc).__name__}", file=sys.stderr)
            return []
        print(f"[food] 高德搜索失败，回退 Tavily：{amap_error}", file=sys.stderr)

    # 2. 回退：Tavily 网页搜索
    query = f"{destination} 美食 必吃 餐厅 小吃"
    if keyword:
        query += f" {keyword}"
    items = _fetch_web_search(query, max_results=min(max_results, 20))
    items = _extract_item_features(items, FOOD_FEATURE_PROMPT)
    for it in items:
        it["_source"] = "tavily"
    return items


def run_food_search(input_data: dict) -> dict:
    """第二阶段：按已规划的午/晚餐位置查询，缺少有效锚点时不搜全城。

    输入 anchors 每项含 day/meal/time/anchor_name/longitude/latitude。
    每个锚点先找 1.5km 内餐厅，候选不足 6 家时扩大到 3km、最多 5km。
    预算透传作排序参考，不在搜索阶段硬筛掉更近的餐厅。
    """
    destination = str(input_data.get("destination") or "").strip()
    if not destination:
        raise ValueError("缺少 destination")
    basic = input_data.get("basic") or {}
    keyword = str(input_data.get("food_keyword") or basic.get("food_keyword") or "").strip() or None
    anchors = input_data.get("anchors") or []
    if not isinstance(anchors, list):
        raise ValueError("anchors 必须是数组")
    budget = _estimate_food_budget(basic)
    valid_anchors: list[tuple[dict, str]] = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        try:
            lng, lat = float(anchor["longitude"]), float(anchor["latitude"])
            if not (-180 <= lng <= 180 and -90 <= lat <= 90) or (lng == 0 and lat == 0):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        location = f"{round(lng, 6)},{round(lat, 6)}"
        valid_anchors.append((anchor, location))

    def fetch_location(location: str) -> list[dict]:
        # 正餐分类规则升级后不能复用含奶茶/糕饼店的旧餐饮缓存。
        cache_key = _cache_key(destination, "food-nearby-v2-meals", location, keyword or "")
        cached = _read_cache(cache_key)
        if cached is not None and isinstance(cached.get("restaurants"), list):
            return cached["restaurants"]
        restaurants: list[dict] = []
        seen: set[str] = set()
        def add_items(items: list[dict]) -> None:
            for item in items:
                identity = str(item.get("poi_id") or f"{item.get('name')}|{item.get('longitude')}|{item.get('latitude')}")
                if identity in seen:
                    continue
                seen.add(identity)
                restaurants.append(item)

        for radius in (1500, 3000, 5000):
            items = _fetch_food(
                destination, keyword, budget, max_results=25,
                location=location, radius=radius, allow_web_fallback=False,
            )
            add_items(items)
            # 高德 keywords 主要匹配名称，“杭帮菜”等偏好可能漏掉本地餐厅。
            # 先补充同一锚点1.5km内的真实餐饮，再考虑把路线扩展到更远处。
            if radius == 1500 and keyword and len(restaurants) < 6:
                add_items(_fetch_food(
                    destination, None, budget, max_results=25,
                    location=location, radius=1500, allow_web_fallback=False,
                ))
            if len(restaurants) >= 6:
                break
        if restaurants:
            _write_cache(cache_key, {"restaurants": restaurants})
        return restaurants

    locations = list(dict.fromkeys(location for _, location in valid_anchors))
    # 两套方案常共享同一景点锚点；只查一次，再按输入顺序返还每个餐点。
    with ThreadPoolExecutor(max_workers=3) as executor:
        found = dict(zip(locations, executor.map(fetch_location, locations)))
    result = [{**anchor, "restaurants": found[location]} for anchor, location in valid_anchors]
    return {"destination": destination, "food_by_anchor": result}


def _fetch_social_food(destination: str, max_results: int = 20) -> list[dict]:
    """在抖音/小红书搜索当地美食视频或笔记链接。"""
    queries = {
        "抖音": f"{destination} 美食 探店 抖音",
        "小红书": f"{destination} 美食 探店 小红书 笔记",
    }
    results: list[dict] = []
    for platform, query in queries.items():
        try:
            items = _fetch_web_search(query, max_results=max_results // 2)
        except Exception:
            items = []
        for item in items:
            url = (item.get("url") or "").lower()
            if platform == "抖音" and "douyin.com" not in url:
                continue
            if platform == "小红书" and "xiaohongshu.com" not in url and "xhslink.com" not in url:
                continue
            results.append(
                {
                    "platform": platform,
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "content": (item.get("content") or "")[:200],
                }
            )
    return results


# ================= 文本格式化（MCP 工具用） =================


def _format_weather(d: dict) -> str:
    lines = [f"{d['location']}（{d['country']}）{d['start_date']} 至 {d['end_date']} 逐日天气："]
    for day in d["days"]:
        lines.append(
            f"  {day['date']}：{day['weather']}，{day['temp_min']}~{day['temp_max']}°C，湿度 {day['humidity']}%"
        )
    return "\n".join(lines) if d["days"] else f"{d['location']} 该日期暂无天气数据。"


def _format_hotels(items: list[dict], destination: str) -> str:
    if not items:
        return f"没有找到「{destination}」的酒店。"
    lines = [f"{destination} 酒店（前 {min(len(items), 5)} 家）："]
    for item in items[:5]:
        parts = [item["name"]]
        for key in ("star", "score", "price", "location"):
            if item.get(key):
                parts.append(str(item[key]))
        lines.append(" - " + " | ".join(parts))
        if item.get("url"):
            lines.append(f"   预订: {item['url']}")
    return "\n".join(lines)


def _format_flights(items: list[dict], origin: str, destination: str | None) -> str:
    label = f"{origin} → {destination or '目的地'}"
    if not items:
        return f"没有找到 {label} 的机票。"
    lines = [f"{label} 机票（前 {min(len(items), 5)} 班）："]
    for item in items[:5]:
        core = f"{item['airline']}{item['flight_no']} | {item['dep_station']}→{item['arr_station']} | {item['dep_time']} → {item['arr_time']}"
        parts = [core]
        if item.get("seat"):
            parts.append(item["seat"])
        if item.get("duration"):
            parts.append(f"{item['duration']}分钟")
        if item.get("price"):
            parts.append(f"¥{item['price']}")
        lines.append(" - " + " | ".join(parts))
        if item.get("url"):
            lines.append(f"   预订: {item['url']}")
    return "\n".join(lines)


def _format_trains(items: list[dict], origin: str, destination: str | None) -> str:
    label = f"{origin} → {destination or '目的地'}"
    if not items:
        return f"没有找到 {label} 的高铁/火车票。"
    lines = [f"{label} 高铁/火车票（前 {min(len(items), 5)} 班）："]
    for item in items[:5]:
        core = f"{item['transport']}{item['train_no']} | {item['dep_station']}→{item['arr_station']} | {item['dep_time']} → {item['arr_time']}"
        parts = [core]
        if item.get("seat"):
            parts.append(item["seat"])
        if item.get("duration"):
            parts.append(f"{item['duration']}分钟")
        if item.get("price"):
            parts.append(f"¥{item['price']}")
        lines.append(" - " + " | ".join(parts))
        if item.get("url"):
            lines.append(f"   预订: {item['url']}")
    return "\n".join(lines)


def _format_poi(items: list[dict], city_name: str) -> str:
    if not items:
        return f"没有找到「{city_name}」的景点。"
    lines = [f"{city_name} 景点（前 {min(len(items), 10)} 个）："]
    for item in items[:10]:
        parts = [item["name"]]
        if item.get("category"):
            parts.append(item["category"])
        if item.get("rank"):
            parts.append(item["rank"])
        if item.get("free"):
            parts.append("免费")
        lines.append(" - " + " | ".join(parts))
        if item.get("description"):
            lines.append(f"   {item['description'][:100]}")
        if item.get("url"):
            lines.append(f"   预订: {item['url']}")
    return "\n".join(lines)


def _format_promotions(items: list[dict], keyword: str) -> str:
    if not items:
        return f"没有找到「{keyword}」相关的促销活动。"
    lines = [f"飞猪促销活动（前 {min(len(items), 10)} 个）："]
    for item in items[:10]:
        parts = [item["title"]]
        if item.get("price"):
            parts.append(str(item["price"]))
        if item.get("star"):
            parts.append(str(item["star"]))
        lines.append(" - " + " | ".join(parts))
        if item.get("url"):
            lines.append(f"   预订: {item['url']}")
    return "\n".join(lines)


def _format_web_search(items: list[dict], query: str) -> str:
    if not items:
        return f"没有找到「{query}」相关的网页结果。"
    lines = [f"「{query}」网页搜索结果（前 {min(len(items), 10)} 条）："]
    for item in items[:10]:
        lines.append(f" - {item['title']}")
        if item.get("content"):
            lines.append(f"   {item['content'][:180]}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
    return "\n".join(lines)


def _format_events(items: list[dict], destination: str, start_date: str, end_date: str) -> str:
    label = f"{_normalize_date(start_date)} 至 {_normalize_date(end_date)}"
    if not items:
        return f"没有找到「{destination} {label}」的热点活动。"
    lines = [f"{destination} 热点活动（{label}，前 {min(len(items), 10)} 条）："]
    for item in items[:10]:
        lines.append(f" - {item['title']}")
        if item.get("content"):
            lines.append(f"   {item['content'][:180]}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
    return "\n".join(lines)


def _format_food(items: list[dict], destination: str) -> str:
    if not items:
        return f"没有找到「{destination}」的餐厅。"
    # 高德结构化数据有 name/cuisine/rating 等字段；Tavily 回退数据只有 title/content
    is_amap = any("cuisine" in item for item in items)
    lines = [f"{destination} 餐厅推荐（前 {min(len(items), 10)} 家）："]
    for item in items[:10]:
        if is_amap:
            parts = [item.get("name", "")]
            if item.get("cuisine"):
                parts.append(f"菜系:{item['cuisine']}")
            if item.get("rating"):
                parts.append(f"评分:{item['rating']}")
            if item.get("price_per_person"):
                parts.append(f"人均:¥{item['price_per_person']}")
            if item.get("business_area"):
                parts.append(f"商圈:{item['business_area']}")
            if item.get("address"):
                parts.append(f"地址:{item['address']}")
            lines.append(" - " + " | ".join(p for p in parts if p))
            if item.get("map_url"):
                lines.append(f"   地图: {item['map_url']}")
            if item.get("poi_detail_url"):
                lines.append(f"   详情: {item['poi_detail_url']}")
        else:
            lines.append(f" - {item.get('title', '')}")
            if item.get("content"):
                lines.append(f"   {item['content'][:180]}")
            if item.get("url"):
                lines.append(f"   {item['url']}")
    return "\n".join(lines)


# ================= MCP 工具（返回文本） =================


@server.tool(
    description="查询某城市在指定日期（段）内的逐日天气。city 必填，start_date/end_date 可选（YYYY-MM-DD）。"
)
def get_weather(city: str, start_date: str | None = None, end_date: str | None = None) -> str:
    return _format_weather(_fetch_weather(city, start_date, end_date))


@server.tool(description="搜索目的地酒店。destination 必填，其余可选。")
def search_hotels(
    destination: str,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    max_price: float | None = None,
    hotel_stars: str | None = None,
    hotel_types: str | None = None,
    sort: str | None = None,
) -> str:
    return _format_hotels(
        _fetch_hotels(destination, check_in_date, check_out_date, max_price, hotel_stars, hotel_types, sort),
        destination,
    )


@server.tool(description="搜索机票。origin 必填，其余可选。")
def search_flights(
    origin: str,
    destination: str | None = None,
    dep_date: str | None = None,
    back_date: str | None = None,
    journey_type: str | None = None,
    sort_type: str | None = None,
    max_price: float | None = None,
) -> str:
    return _format_flights(
        _fetch_flights(origin, destination, dep_date, back_date, journey_type, sort_type, max_price),
        origin,
        destination,
    )


@server.tool(description="搜索高铁/火车票。origin 必填，其余可选。")
def search_trains(
    origin: str,
    destination: str | None = None,
    dep_date: str | None = None,
    sort_type: str | None = None,
) -> str:
    return _format_trains(
        _fetch_trains(origin, destination, dep_date, sort_type),
        origin,
        destination,
    )


@server.tool(description="搜索景点/风景名胜。city_name 必填，其余可选。")
def search_poi(
    city_name: str,
    keyword: str | None = None,
    category: str | None = None,
    poi_level: str | None = None,
) -> str:
    return _format_poi(_fetch_poi(city_name, keyword, category, poi_level), city_name)


@server.tool(description="检索飞猪促销活动/优惠商品（特价机票卡、券包、酒店套餐等）。keyword 可选。")
def search_promotions(keyword: str | None = None) -> str:
    return _format_promotions(_fetch_promotions(keyword), keyword or "促销活动")


@server.tool(
    description="通用网页搜索（Tavily），返回 LLM 优化的搜索结果摘要。query 必填，max_results 可选（默认 10）。"
)
def search_web(query: str, max_results: int = 10) -> str:
    return _format_web_search(_fetch_web_search(query, max_results), query)


@server.tool(
    description="搜索某地在日期段内的热点活动（演唱会/音乐节/比赛/展览/节日等）。destination、start_date、end_date 必填（YYYY-MM-DD）。"
)
def search_events(destination: str, start_date: str, end_date: str, max_results: int = 10) -> str:
    return _format_events(
        _fetch_events(destination, start_date, end_date, max_results),
        destination,
        start_date,
        end_date,
    )


@server.tool(description="搜索目的地餐厅（高德地图），返回名称、菜系、评分、人均、地址、商圈和地图/详情链接。destination 必填。")
def search_food(destination: str, max_results: int = 10) -> str:
    return _format_food(_fetch_food(destination, max_results=max_results), destination)


# ================= 确定性综合编排（JSON in / JSON out） =================


def _run_safe(key: str, fn: Any) -> tuple[str, Any]:
    try:
        return key, fn()
    except Exception as exc:  # noqa: BLE001
        return key, {"error": str(exc)}


def _cache_key(destination: str, start: str, end: str, origin: str, extra: str = "") -> str:
    raw = "|".join([SEARCH_VERSION, destination, start, end, origin, extra])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(key: str, result: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _ensure_requested_pois(result: dict, destination: str, requested_pois: list[str]) -> list[str]:
    """把用户点名但搜索结果里没有的景点单独补搜并入 poi；返回仍未找到的点名。"""
    requested_pois = [str(name).strip() for name in (requested_pois or []) if str(name).strip()]
    if not requested_pois:
        return []
    pois = result.get("poi")
    if not isinstance(pois, list):
        pois = []
        result["poi"] = pois
    existing = {str(p.get("name") or "").strip() for p in pois if str(p.get("name") or "").strip()}
    missing: list[str] = []
    for name in dict.fromkeys(requested_pois):
        if any(_exact_poi_match(name, candidate) for candidate in existing):
            continue
        try:
            extra = _fetch_poi(destination, keyword=name)
        except Exception:
            extra = []
        # 只取一个最佳匹配，避免把「武汉大学樱花大道/万林艺术博物馆」等子景点都塞进必去。
        match = next(
            (item for item in extra if _exact_poi_match(name, str(item.get("name") or ""))),
            None,
        )
        if match is None:
            match = next((item for item in extra if str(item.get("name") or "").strip()), None)
        if match is None:
            missing.append(name)
            continue
        item_name = str(match.get("name") or "").strip()
        if item_name not in existing:
            existing.add(item_name)
            match["requested"] = True
            match["must_visit"] = True
            match["source"] = "on_demand"
            match["category_label"] = _category_label(match)
            pois.append(match)
    return missing


def run_search(input_data: dict) -> dict:
    """输入 {destination, start_date, end_date?, origin?, basic?, food_keyword?}，
    并行搜索天气、酒店、景点、活动和交通，返回结构化 JSON。

    basic 可含 total_budget（总预算）、travelers（出行人数）、purposes（旅行目的）。
    此阶段也做一次全城餐厅搜索（仅用于前端候选展示）；规划仍用景点确定后的周边搜索。
    """
    destination = (input_data.get("destination") or "").strip()
    if not destination:
        raise ValueError("缺少 destination")
    start_date = input_data.get("start_date")
    if not start_date:
        raise ValueError("缺少 start_date")
    start = _normalize_date(start_date)
    end = _normalize_date(input_data["end_date"]) if input_data.get("end_date") else start
    origin = (input_data.get("origin") or "").strip()

    # 从 basic 中提取餐饮搜索参数
    basic = {**(input_data.get("basic") or {}), "start_date": start, "end_date": end}
    food_keyword = str(input_data.get("food_keyword") or basic.get("food_keyword") or "").strip() or None
    max_price = _estimate_food_budget(basic)
    profile_styles = (input_data.get("profile") or {}).get("travel_style") or []
    if isinstance(profile_styles, str):
        profile_styles = [profile_styles]
    trip_styles = basic.get("travel_style") or []
    if isinstance(trip_styles, str):
        trip_styles = [trip_styles]
    travel_styles = list(dict.fromkeys(list(profile_styles) + list(trip_styles)))
    requested_pois = basic.get("requested_pois") or []
    if isinstance(requested_pois, str):
        requested_pois = [requested_pois]
    requested_pois = [str(name).strip() for name in requested_pois if str(name).strip()]
    requested_pois = list(dict.fromkeys(requested_pois))

    # 缓存需区分 travel_style（不同风格会多取/少取不同类别的景点）。
    cache_key = _cache_key(
        destination, start, end, origin,
        extra=json.dumps(
            {
                "food": food_keyword,
                "budget": max_price,
                "travel_styles": sorted(travel_styles),
                "requested_pois": requested_pois,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    cached = _read_cache(cache_key)
    if cached is not None:
        return cached

    result: dict[str, Any] = {
        "destination": destination,
        "start_date": start,
        "end_date": end,
    }
    if food_keyword:
        result["food_keyword"] = food_keyword
    if origin:
        result["origin"] = origin

    tasks = {
        "weather": lambda: _fetch_weather(destination, start, end),
        "hotels": lambda: _fetch_hotels(destination, start, end) if start != end else [],
        "poi": lambda: _fetch_poi_distributed(destination, target=50, travel_styles=travel_styles),
        "events": lambda: _fetch_events(destination, start, end),
        "social_food": lambda: _fetch_social_food(destination),
        "food": lambda: _fetch_food(destination, keyword=food_keyword, max_price=max_price, max_results=30),
    }
    if origin and origin != destination:
        tasks["flights"] = lambda: _fetch_round_trip(_fetch_flights, origin, destination, start, end)
        tasks["trains"] = lambda: _fetch_round_trip(_fetch_trains, origin, destination, start, end)
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {key: executor.submit(_run_safe, key, fn) for key, fn in tasks.items()}
        for key, future in futures.items():
            result_key, value = future.result()
            result[result_key] = value

    # 景点日程确定后，由 PlanAgent 用实际餐点位置调用 --food-nearby。
    result["food_search_pending"] = True

    # 飞猪数据源偶发抖动会整批返回空，重试一次并避免把空结果缓存 1 小时。
    if not isinstance(result.get("poi"), list) or not result["poi"]:
        result["poi"] = _fetch_poi_distributed(destination, target=50, travel_styles=travel_styles)
    # 用户点名但榜单/分类结果里没有的景点，用关键词单独补搜。
    missing_requested = _ensure_requested_pois(result, destination, requested_pois)
    if missing_requested:
        result.setdefault("warnings", [])
        result["warnings"].append(
            "未能搜索到用户点名景点：" + "、".join(missing_requested) + "。"
        )
    if isinstance(result.get("poi"), list) and result["poi"]:
        _write_cache(cache_key, result)
    return result


def _estimate_food_budget(basic: dict) -> float | None:
    """按实际人数、天数，将总预算的 25% 分摊到每人每天 3 餐。"""
    total_budget = basic.get("total_budget")
    if not total_budget:
        return None
    try:
        total = float(total_budget)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(total) or total <= 0:
        return None

    travelers = 1
    travelers_raw = basic.get("travelers") or ""
    m = re.search(r"(\d+)", str(travelers_raw))
    if m:
        travelers = max(1, int(m.group(1)))

    try:
        days = max(1, int(basic.get("days") or basic.get("duration_days") or 0))
    except (TypeError, ValueError):
        days = 1
    if basic.get("start_date") and basic.get("end_date"):
        try:
            days = max(1, (date.fromisoformat(basic["end_date"]) - date.fromisoformat(basic["start_date"])).days + 1)
        except (ValueError, TypeError):
            pass
    per_meal = total * 0.25 / travelers / (days * 3)
    return round(per_meal, 0)


@server.tool(description="综合搜索目的地天气、酒店、景点、活动和交通；餐厅在行程确定后按景点周边搜索。")
def search_trip(destination: str, start_date: str, end_date: str | None = None) -> str:
    return json.dumps(
        run_search({"destination": destination, "start_date": start_date, "end_date": end_date}),
        ensure_ascii=False,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
