"""SearchAgent 的工具服务（MCP server）。

通过 MCP 协议对外暴露六个工具：

- ``get_weather``：查询某日期/日期段的逐日天气（Open-Meteo，无需 Key）
- ``search_hotels``：搜索目的地酒店（飞猪 FlyAI）
- ``search_flights``：搜索机票（飞猪 FlyAI）
- ``search_poi``：搜索景点/风景名胜（飞猪 FlyAI）
- ``search_events``：按地点和时间搜索热点活动（演唱会/比赛/节日等，DuckDuckGo 网络搜索）
- ``search_food``：搜索美食/餐厅（大众点评/抖音/小红书等，DuckDuckGo 网络搜索）

运行方式（stdio transport）：``python tools.py``
"""

import json
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import certifi
from ddgs import DDGS
from mcp.server.fastmcp import FastMCP

server = FastMCP(
    "search-tools",
    instructions="SearchAgent 的工具集",
    log_level="ERROR",
)

FLYAI_BIN = Path(__file__).resolve().parent / "node_modules" / ".bin" / "flyai"


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

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
    """把 LLM 可能传的各种日期格式统一成 YYYY-MM-DD。"""
    value = value.strip()
    # 2026-10-1 / 2026-10-01
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 2026/10/1 或 2026.10.1
    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 2026年10月1日 / 2026年10月1号
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 10-1 / 10/1 / 10.1（补当前年份）
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", value)
    if m:
        return f"{date.today().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    # 10月1日 / 10月1号（补当前年份）
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})[日号]?", value)
    if m:
        return f"{date.today().year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return value


def _web_search(queries: list[str], max_per_query: int = 15) -> list[dict]:
    """对多组查询词做网络搜索并去重，返回最多 50 条结果（带重试退避，应对限流）。"""
    items: list[dict] = []
    seen: set[str] = set()
    with DDGS() as ddgs:
        for query in queries:
            results: list[dict] = []
            for attempt in range(3):
                try:
                    results = list(
                        ddgs.text(query, max_results=max_per_query, region="cn-zh")
                    )
                    if results or attempt == 2:
                        break
                except Exception:
                    pass
                time.sleep(2 * (attempt + 1))
            for result in results:
                url = (result.get("href") or result.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                items.append(
                    {
                        "title": (result.get("title") or "").strip(),
                        "snippet": (result.get("body") or "").strip(),
                        "url": url,
                    }
                )
                if len(items) >= 50:
                    return items
            time.sleep(1)
    return items


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


@server.tool(
    description=(
        "查询某城市在指定日期（或日期段）内的逐日天气。"
        "city 为城市名（支持中英文，必填）；start_date 为开始日期（YYYY-MM-DD，可选，缺省今天）；"
        "end_date 为结束日期（可选，缺省等于 start_date）。"
        "返回每天的天气状况、最高/最低气温和湿度。"
    )
)
def get_weather(
    city: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """返回某城市某日期段的逐日天气。"""
    latitude, longitude, name, country = _geocode_city(city)

    today = date.today()
    try:
        start = date.fromisoformat(_normalize_date(start_date)) if start_date else today
        end = date.fromisoformat(_normalize_date(end_date)) if end_date else start
    except ValueError:
        return f"日期无法识别（{start_date or end_date}），请用 YYYY-MM-DD 格式。"

    base_url = (
        "https://archive-api.open-meteo.com/v1/archive"
        if end < today
        else FORECAST_URL
    )
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
    if not times:
        return f"{name}（{country}）该日期暂无天气数据。"

    if start == end:
        title = f"{name}（{country}）{start.isoformat()} 天气："
    else:
        title = f"{name}（{country}）{start.isoformat()} 至 {end.isoformat()} 逐日天气："

    lines = [title]
    for i, day in enumerate(times):
        code = daily.get("weather_code", [])[i]
        description = WEATHER_CODES.get(code, f"代码{code}")
        tmax = daily.get("temperature_2m_max", [])[i]
        tmin = daily.get("temperature_2m_min", [])[i]
        humidity = daily.get("relative_humidity_2m_mean", [])[i]
        lines.append(f"  {day}：{description}，{tmin}~{tmax}°C，湿度 {humidity}%")
    return "\n".join(lines)


@server.tool(
    description=(
        "搜索指定目的地的酒店。destination 为目的地（城市/省/国家/区，必填）；"
        "可选入住日期 check_in_date、离店日期 check_out_date（格式 YYYY-MM-DD）、"
        "最高每晚价格 max_price、星级 hotel_stars（逗号分隔，如 4,5）、"
        "酒店类型 hotel_types（酒店/民宿/客栈）、排序 sort。返回酒店名称、价格、星级、位置与预订链接。"
    )
)
def search_hotels(
    destination: str,
    check_in_date: str | None = None,
    check_out_date: str | None = None,
    max_price: float | None = None,
    hotel_stars: str | None = None,
    hotel_types: str | None = None,
    sort: str | None = None,
) -> str:
    """搜索目的地酒店并返回简要结果。"""
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
        return f"查询出错：{data.get('message') or '未知错误'}"
    items = (data.get("data") or {}).get("itemList") or []
    if not items:
        return f"没有找到「{destination}」的酒店，换个目的地或放宽条件试试。"

    lines = [f"{destination} 酒店（前 {min(len(items), 5)} 家）："]
    for item in items[:5]:
        parts = [item.get("name") or "未知酒店"]
        if item.get("star"):
            parts.append(str(item["star"]))
        if item.get("scoreDesc") or item.get("score"):
            parts.append(str(item.get("scoreDesc") or item.get("score")))
        if item.get("price"):
            parts.append(str(item["price"]))
        poi = item.get("interestsPoi") or item.get("address") or ""
        if poi:
            parts.append(poi)
        lines.append(" - " + " | ".join(parts))
        if item.get("detailUrl"):
            lines.append(f"   预订: {item['detailUrl']}")
    return "\n".join(lines)


@server.tool(
    description=(
        "搜索机票。origin 为出发城市/机场（必填），destination 为目的地城市/机场；"
        "可选出发日期 dep_date、返程日期 back_date（格式 YYYY-MM-DD）、"
        "journey_type 直飞/中转（1=直飞，2=中转）、sort_type 排序、max_price 最高价格。"
        "返回航班、时刻、舱位、价格与预订链接。"
    )
)
def search_flights(
    origin: str,
    destination: str | None = None,
    dep_date: str | None = None,
    back_date: str | None = None,
    journey_type: str | None = None,
    sort_type: str | None = None,
    max_price: float | None = None,
) -> str:
    """搜索机票并返回简要结果。"""
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
        return f"查询出错：{data.get('message') or '未知错误'}"
    items = (data.get("data") or {}).get("itemList") or []
    label = f"{origin} → {destination or '目的地'}"
    if not items:
        return f"没有找到 {label} 的机票。"

    lines = [f"{label} 机票（前 {min(len(items), 5)} 班）："]
    for item in items[:5]:
        journeys = item.get("journeys") or []
        segment = (journeys[0].get("segments") or [{}])[0] if journeys else {}
        airline = segment.get("marketingTransportName") or ""
        flight_no = segment.get("marketingTransportNo") or ""
        dep_station = segment.get("depStationName") or ""
        arr_station = segment.get("arrStationName") or ""
        dep_time = segment.get("depDateTime") or ""
        arr_time = segment.get("arrDateTime") or ""
        seat = segment.get("seatClassName") or ""

        core = f"{airline}{flight_no} | {dep_station}→{arr_station} | {dep_time} → {arr_time}"
        parts = [core]
        if seat:
            parts.append(seat)
        if item.get("totalDuration"):
            parts.append(f"{item['totalDuration']}分钟")
        price = item.get("ticketPrice") or item.get("adultPrice") or ""
        if price:
            parts.append(f"¥{price}")
        lines.append(" - " + " | ".join(parts))
        if item.get("jumpUrl"):
            lines.append(f"   预订: {item['jumpUrl']}")
    return "\n".join(lines)


@server.tool(
    description=(
        "按地点和时间搜索该时间段内的热点活动（演唱会、音乐节、体育比赛、节日庆典、展览演出等）。"
        "location 为地点（城市名，必填）；start_date 为开始日期（YYYY-MM-DD，必填）；"
        "end_date 为结束日期（可选，缺省等于 start_date）；keywords 可选，用于指定活动类型（逗号分隔）。"
        "返回去重后的活动列表（最多 50 条），含标题、摘要和链接。"
    )
)
def search_events(
    location: str,
    start_date: str,
    end_date: str | None = None,
    keywords: str | None = None,
) -> str:
    """搜索某地点在某时间段内的热点活动。"""
    start = _normalize_date(start_date)
    end = _normalize_date(end_date) if end_date else start

    sy, sm, _ = (int(x) for x in start.split("-"))
    ey, em, _ = (int(x) for x in end.split("-"))
    if (sy, sm) == (ey, em):
        period = f"{sy}年{sm}月"
    else:
        period = f"{sy}年{sm}月至{ey}年{em}月"

    cats = [c.strip() for c in (keywords or "").split(",") if c.strip()]
    if not cats:
        cats = ["演唱会 音乐节", "体育比赛 赛事", "节日 庆典 活动", "展览 演出"]
    queries = [f"{location} {period} {cat}" for cat in cats]

    items = _web_search(queries)
    if not items:
        return f"没有搜到「{location}」在 {period} 的热点活动，换个地点或日期试试。"

    lines = [f"{location} {period} 热点活动（共 {len(items)} 条）："]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item['title']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:100]}")
        lines.append(f"   {item['url']}")
    return "\n".join(lines)


@server.tool(
    description=(
        "搜索某城市的景点/风景名胜。city_name 为城市名（必填）；"
        "keyword 为景点名称关键词（可选，如 西湖、故宫）；"
        "category 为景点类别（可选，如 自然风光、人文古迹、历史古迹、山湖田园、古镇古村、宗教场所、博物馆 等）；"
        "poi_level 为景点等级 1-5（可选）。返回景点名称、类别、排名、地址、简介和预订链接。"
    )
)
def search_poi(
    city_name: str,
    keyword: str | None = None,
    category: str | None = None,
    poi_level: str | None = None,
) -> str:
    """搜索某城市的景点/风景名胜。"""
    args = ["search-poi", "--city-name", city_name]
    if keyword:
        args += ["--keyword", keyword]
    if category:
        args += ["--category", category]
    if poi_level:
        args += ["--poi-level", poi_level]

    data = _run_flyai(args)
    if data.get("status") not in (0, None):
        return f"查询出错：{data.get('message') or '未知错误'}"
    items = (data.get("data") or {}).get("itemList") or []
    if not items:
        return f"没有找到「{city_name}」的景点，换个关键词或类别试试。"

    lines = [f"{city_name} 景点（前 {min(len(items), 10)} 个）："]
    for item in items[:10]:
        parts = [item.get("name") or "未知景点"]
        if item.get("category"):
            parts.append(str(item["category"]))
        if item.get("listRank"):
            parts.append(str(item["listRank"]))
        if item.get("freePoiStatus") == "FREE":
            parts.append("免费")
        lines.append(" - " + " | ".join(parts))
        if item.get("description"):
            lines.append(f"   {item['description'][:100]}")
        if item.get("jumpUrl"):
            lines.append(f"   预订: {item['jumpUrl']}")
    return "\n".join(lines)


@server.tool(
    description=(
        "搜索某地的美食/餐厅，重点在大众点评、抖音、小红书等社交平台上检索。"
        "location 为地点（城市/区域，必填）；keywords 为菜系或类型（可选，如 杭帮菜、火锅、小吃）。"
        "返回去重后的美食相关条目（最多 50 条），含标题、摘要和链接。"
    )
)
def search_food(
    location: str,
    keywords: str | None = None,
) -> str:
    """搜索某地的美食/餐厅。"""
    food = (keywords or "").strip() or "美食"
    queries = [
        f"site:dianping.com {location} {food}",
        f"site:xiaohongshu.com {location} {food}",
        f"site:douyin.com {location} {food}",
        f"{location} {food} 大众点评 必吃榜",
        f"{location} {food} 小红书 探店",
        f"{location} {food} 抖音 探店",
    ]
    items = _web_search(queries)
    if not items:
        return f"没有搜到「{location}」的{food}相关内容，换个地点或关键词试试。"

    lines = [f"{location} {food} 相关推荐（共 {len(items)} 条）："]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item['title']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:100]}")
        lines.append(f"   {item['url']}")
    return "\n".join(lines)


def main() -> None:
    server.run()  # 默认使用 stdio transport


if __name__ == "__main__":
    main()
