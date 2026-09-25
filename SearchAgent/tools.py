"""SearchAgent 的工具服务（MCP server）与确定性搜索编排。

结构：
- 结构化核心函数 ``_fetch_*``：返回 dict / list[dict]，供 JSON 输出与编排使用
- 文本格式化函数 ``_format_*``：把结构化数据转成可读文本
- MCP 工具：把结构化数据格式化成文本，供 LLM agent 调用
- ``run_search``：确定性综合编排，输入 JSON 输出 JSON（不经过 LLM）
"""

import json
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import certifi
from mcp.server.fastmcp import FastMCP

server = FastMCP(
    "search-tools",
    instructions="SearchAgent 的工具集",
    log_level="ERROR",
)

FLYAI_BIN = Path(__file__).resolve().parent / "node_modules" / ".bin" / "flyai"

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
    cmd = [str(FLYAI_BIN), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
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


def _fetch_hotels(
    destination: str,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    max_price: float | None = None,
    hotel_stars: str | None = None,
    hotel_types: str | None = None,
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
        }
        for item in items
    ]


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


def _fetch_poi(
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
        args += ["--poi-level", poi_level]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        raise ValueError(data.get("message") or "查询出错")
    items = (data.get("data") or {}).get("itemList") or []
    return [
        {
            "name": item.get("name") or "",
            "category": item.get("category") or "",
            "rank": item.get("listRank") or "",
            "free": item.get("freePoiStatus") == "FREE",
            "description": item.get("description") or "",
            "url": item.get("jumpUrl") or "",
        }
        for item in items
    ]


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


# ================= 确定性综合编排（JSON in / JSON out） =================


def _run_safe(key: str, fn: Any) -> tuple[str, Any]:
    try:
        return key, fn()
    except Exception as exc:  # noqa: BLE001
        return key, {"error": str(exc)}


def run_search(input_data: dict) -> dict:
    """输入 {destination, start_date, end_date?}，并行调用 4 个工具，返回结构化 JSON。"""
    destination = (input_data.get("destination") or "").strip()
    if not destination:
        raise ValueError("缺少 destination")
    start_date = input_data.get("start_date")
    if not start_date:
        raise ValueError("缺少 start_date")
    start = _normalize_date(start_date)
    end = _normalize_date(input_data["end_date"]) if input_data.get("end_date") else start

    result: dict[str, Any] = {
        "destination": destination,
        "start_date": start,
        "end_date": end,
    }

    tasks = {
        "weather": lambda: _fetch_weather(destination, start, end),
        "hotels": lambda: _fetch_hotels(destination, start, end),
        "poi": lambda: _fetch_poi(destination),
        "promotions": lambda: _fetch_promotions(f"{destination} 促销 特价"),
    }
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {key: executor.submit(_run_safe, key, fn) for key, fn in tasks.items()}
        for key, future in futures.items():
            result_key, value = future.result()
            result[result_key] = value

    return result


@server.tool(description="综合搜索某目的地（天气+酒店+景点+促销），返回结构化 JSON。")
def search_trip(destination: str, start_date: str, end_date: str | None = None) -> str:
    return json.dumps(
        run_search({"destination": destination, "start_date": start_date, "end_date": end_date}),
        ensure_ascii=False,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
