"""路线补全：用高德 Web 服务 API 给 blocks 补坐标，并计算相邻地点间的真实路线。

- 地理编码：/v5/place/text 按「城市 + 名称」查 POI，拿到 GCJ-02 坐标
- 路线：按直线距离选择 步行 / 公交地铁 / 驾车，返回距离、耗时、折线
- 结果缓存在 route_cache.json，同名地点和同一段路线不重复请求
未配置 AMAP_KEY 或请求失败时静默跳过，不影响计划生成。
"""

import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.route_timing import LOCAL_TRANSFER_PADDING_S, route_seconds

AMAP_BASE = "https://restapi.amap.com"
CACHE_PATH = Path(__file__).resolve().parent / "route_cache.json"
WALK_MAX_KM = 1.2
TRANSIT_MAX_KM = 25
# 高德个人开发者 key 的 QPS 较低（约 1~3），串行 + 限速避免触发限流重试
MAX_WORKERS = 1
REQUEST_GAP = 0.25  # 每次请求间隔（秒），控制到约 4 QPS 以内

_lock = threading.RLock()
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _cache = {}
            _cache.setdefault("geo", {})
            _cache.setdefault("leg", {})
            _cache.setdefault("walking", {})
        return _cache


def _save_cache() -> None:
    with _lock:
        CACHE_PATH.write_text(json.dumps(_load_cache(), ensure_ascii=False), encoding="utf-8")


def _get(path: str, **params) -> dict:
    params["key"] = os.getenv("AMAP_KEY", "")
    url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(params)}"
    context = ssl.create_default_context(cafile=certifi.where())
    time.sleep(REQUEST_GAP)
    for attempt in range(2):
        with urllib.request.urlopen(url, timeout=10, context=context) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "1":
            return data
        # 10021/10019/10020：QPS 超限，稍等重试一次
        if data.get("infocode") in ("10019", "10020", "10021") and attempt == 0:
            time.sleep(0.5)
            continue
        raise RuntimeError(f"amap {path}: {data.get('info')} ({data.get('infocode')})")
    return {}


def _km(a: str, b: str) -> float:
    x1, y1 = map(float, a.split(","))
    x2, y2 = map(float, b.split(","))
    dx = math.radians(x2 - x1) * math.cos(math.radians((y1 + y2) / 2))
    dy = math.radians(y2 - y1)
    return 6371 * math.hypot(dx, dy)


def _points(raw) -> list[list[float]]:
    """高德折线 "lng,lat;lng,lat" → [[lng, lat], ...]；v5 公交里折线可能包在 {"polyline": ...} 里。"""
    if isinstance(raw, dict):
        raw = raw.get("polyline")
    if not isinstance(raw, str):
        return []
    pts = []
    for pair in raw.split(";"):
        if "," in pair:
            lng, lat = pair.split(",")[:2]
            pts.append([round(float(lng), 5), round(float(lat), 5)])
    return pts


def geocode(name: str, city: str) -> dict | None:
    cache = _load_cache()["geo"]
    key = f"{city}|{name}"
    if key in cache:
        return cache[key]
    try:
        pois = _get(
            "/v5/place/text", keywords=name, region=city, city_limit="true", page_size=1
        ).get("pois") or []
    except Exception:  # noqa: BLE001
        return None  # 网络/配额错误不写缓存，下次再试
    hit = None
    if pois and pois[0].get("location"):
        p = pois[0]
        hit = {"location": p["location"], "poi_id": p.get("id") or "", "citycode": p.get("citycode") or ""}
    with _lock:
        cache[key] = hit
    return hit


def _walk_or_drive(path: str, origin: str, dest: str, mode: str) -> dict:
    route = _get(path, origin=origin, destination=dest, show_fields="cost,polyline")["route"]
    p = route["paths"][0]
    return {
        "mode": mode,
        "distance_m": int(float(p.get("distance") or 0)),
        "duration_s": int(float((p.get("cost") or {}).get("duration") or 0)),
        "lines": [],
        "polyline": [pt for s in p.get("steps") or [] for pt in _points(s.get("polyline"))],
    }


def _transit(origin: str, dest: str, citycode: str) -> dict | None:
    route = _get(
        "/v5/direction/transit/integrated",
        origin=origin,
        destination=dest,
        city1=citycode,
        city2=citycode,
        show_fields="cost,polyline",
    ).get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        return None
    t = transits[0]
    line: list[list[float]] = []
    names: list[str] = []
    for seg in t.get("segments") or []:
        for step in (seg.get("walking") or {}).get("steps") or []:
            line += _points(step.get("polyline"))
        for bus in ((seg.get("bus") or {}).get("buslines") or [])[:1]:
            names.append((bus.get("name") or "").split("(")[0])
            line += _points(bus.get("polyline"))
    return {
        "mode": "transit",
        "distance_m": int(float(t.get("distance") or 0)),
        "duration_s": int(float((t.get("cost") or {}).get("duration") or 0)),
        "lines": [n for n in names if n],
        "polyline": line,
    }


def _is_metro_line(name: str, typ: str | None = None) -> bool:
    """判断一条公交线路是否为地铁/轨道交通。"""
    text = f"{name or ''} {typ or ''}"
    return any(key in text for key in ("地铁", "轨道", "轻轨", "号线"))


def _transit_modes(origin: str, dest: str, citycode: str) -> list[dict]:
    """返回「地铁（若有）+ 公交」两种方案，分别取耗时最短的一条。"""
    route = _get(
        "/v5/direction/transit/integrated",
        origin=origin,
        destination=dest,
        city1=citycode,
        city2=citycode,
        show_fields="cost,polyline",
    ).get("route") or {}
    transits = route.get("transits") or []
    metro: dict | None = None
    bus: dict | None = None
    for t in transits:
        names: list[str] = []
        is_metro = False
        line: list[list[float]] = []
        for seg in t.get("segments") or []:
            for step in (seg.get("walking") or {}).get("steps") or []:
                line += _points(step.get("polyline"))
            for bl in (seg.get("bus") or {}).get("buslines") or []:
                name = (bl.get("name") or "").split("(")[0]
                if name:
                    names.append(name)
                if _is_metro_line(name, bl.get("type")):
                    is_metro = True
                line += _points(bl.get("polyline"))
        duration = int(float((t.get("cost") or {}).get("duration") or 0))
        if duration <= 0:
            continue
        info = {
            "mode": "metro" if is_metro else "bus",
            "label": "地铁" if is_metro else "公交",
            "distance_m": int(float(t.get("distance") or 0)),
            "duration_s": duration,
            "lines": [n for n in names if n],
            "polyline": line,
        }
        if is_metro and (metro is None or duration < metro["duration_s"]):
            metro = info
        elif not is_metro and (bus is None or duration < bus["duration_s"]):
            bus = info
    return [o for o in (metro, bus) if o]


def _bicycling(origin: str, dest: str) -> dict | None:
    """骑行路线（高德 /v5/direction/bicycling）。"""
    try:
        route = _get(
            "/v5/direction/bicycling",
            origin=origin,
            destination=dest,
            show_fields="cost,polyline",
        )["route"]
        p = route["paths"][0]
        duration = (p.get("cost") or {}).get("duration") or p.get("duration") or 0
        return {
            "mode": "bike",
            "distance_m": int(float(p.get("distance") or 0)),
            "duration_s": int(float(duration or 0)),
            "lines": [],
            "polyline": [pt for s in p.get("steps") or [] for pt in _points(s.get("polyline"))],
        }
    except Exception:  # noqa: BLE001
        return None


def _taxi_fare(distance_m: int, duration_s: int = 0) -> int:
    """粗略估算打车费：起步价 11 元/3km，之后约 2.6 元/km（不含夜间/等待）。"""
    km = max(0.0, int(distance_m or 0) / 1000.0)
    fare = 11.0 + max(0.0, km - 3.0) * 2.6
    return max(11, int(round(fare)))


def walking_route(origin: str, dest: str, cached_only: bool = False) -> dict | None:
    """独立查询步行路线，供餐厅排序使用；不把驾车距离当作步行距离。"""
    cache = _load_cache()
    origin = ",".join(f"{float(value):.6f}" for value in origin.split(","))
    dest = ",".join(f"{float(value):.6f}" for value in dest.split(","))
    key = f"{origin}>{dest}"
    hit = cache["walking"].get(key) or cache["leg"].get(key)
    if hit and hit.get("mode") == "walk":
        return hit
    if cached_only or not os.getenv("AMAP_KEY"):
        return None
    try:
        leg = _walk_or_drive("/v5/direction/walking", origin, dest, "walk")
    except Exception:  # noqa: BLE001
        return None
    if leg.get("distance_m", 0) <= 0 or leg.get("duration_s", 0) <= 0:
        return None
    with _lock:
        cache["walking"][key] = leg
    return leg


def driving_travel(origin: str, dest: str) -> dict | None:
    """返回 origin→dest 的打车（驾车）耗时路线（带内存缓存）。"""
    if not os.getenv("AMAP_KEY"):
        return None
    origin = ",".join(f"{float(value):.6f}" for value in origin.split(","))
    dest = ",".join(f"{float(value):.6f}" for value in dest.split(","))
    key = f"drive:{origin}>{dest}"
    cache = _load_cache()
    hit = cache["leg"].get(key)
    if hit:
        return hit
    try:
        leg = _walk_or_drive("/v5/direction/driving", origin, dest, "drive")
    except Exception:  # noqa: BLE001
        return None
    if not leg or (leg.get("duration_s") or 0) <= 0:
        return None
    with _lock:
        cache["leg"][key] = leg
    return leg


def route_leg(a: dict, b: dict) -> dict | None:
    """计算 a→b 的多种交通方式：地铁（若有）、公交、打车（含价格）、<3km 骑行。"""
    cache = _load_cache()["leg"]
    origin = ",".join(f"{float(value):.6f}" for value in a["location"].split(","))
    dest = ",".join(f"{float(value):.6f}" for value in b["location"].split(","))
    key = f"{origin}>{dest}"
    cached = cache.get(key)
    if cached and isinstance(cached.get("options"), list):
        return cached
    km = _km(a["location"], b["location"])
    options: list[dict] = []
    if km < TRANSIT_MAX_KM and a.get("citycode"):
        try:
            options.extend(_transit_modes(a["location"], b["location"], a["citycode"]))
        except Exception:  # noqa: BLE001
            pass
    drive: dict | None = None
    try:
        drive = _walk_or_drive("/v5/direction/driving", a["location"], b["location"], "drive")
    except Exception:  # noqa: BLE001
        drive = None
    if drive and (drive.get("duration_s") or 0) > 0:
        drive["label"] = "打车"
        drive["price"] = _taxi_fare(drive.get("distance_m") or 0, drive.get("duration_s") or 0)
        options.append(drive)
    if km < 3.0:
        bike = _bicycling(a["location"], b["location"])
        if bike and (bike.get("duration_s") or 0) > 0:
            bike["label"] = "骑行"
            options.append(bike)
    if not options:
        return None
    # 地图折线优先用打车路线（连续道路），没有打车方案则退回首个可用方案。
    primary = next((o for o in options if o.get("mode") == "drive"), options[0])
    result = {
        "mode": primary.get("mode") or "drive",
        "distance_m": primary.get("distance_m") or 0,
        "duration_s": primary.get("duration_s") or 0,
        "lines": primary.get("lines") or [],
        "polyline": primary.get("polyline") or [],
        "options": [{k: v for k, v in o.items() if k != "polyline"} for o in options],
    }
    with _lock:
        cache[key] = result
    return result


def _local_gap_seconds(a: dict, b: dict) -> int | None:
    if a.get("day") != b.get("day") or a.get("type") in ("交通", "天气", "酒店") or b.get("type") in ("交通", "天气", "酒店"):
        return None
    if a.get("date") and b.get("date") and a["date"] != b["date"]:
        return None
    import re
    pattern = r"\s*(\d{1,2}):(\d{2})\s*[-—–~～至]\s*(\d{1,2}):(\d{2})\s*"
    before, after = re.fullmatch(pattern, str(a.get("time") or "")), re.fullmatch(pattern, str(b.get("time") or ""))
    if not before or not after:
        return None
    values = [int(value) for value in (*before.groups(), *after.groups())]
    if any(hour > 23 or minute > 59 for hour, minute in zip(values[::2], values[1::2])):
        return None
    return ((values[4] * 60 + values[5]) - (values[2] * 60 + values[3])) * 60


def _route_for_schedule(a: dict, b: dict) -> dict | None:
    leg = route_leg(a["_geo"], b["_geo"])
    gap = _local_gap_seconds(a, b)
    duration = route_seconds(leg)
    if (not leg or gap is None or gap <= 0 or duration is None or
            duration + LOCAL_TRANSFER_PADDING_S <= gap or leg.get("mode") not in ("walk", "transit") or
            leg.get("mode_locked") is True):
        return leg
    driving = driving_travel(a["_geo"]["location"], b["_geo"]["location"])
    driving_seconds = route_seconds(driving)
    if driving_seconds is None:
        return leg
    # Keep the coordinate cache's default independent of a particular itinerary.
    # The chosen faster route and its polyline are sent to both map and reviewer.
    selected, alternative = (driving, leg) if driving_seconds < duration else (leg, driving)
    result = dict(selected)
    if selected is driving:
        result["selection_reason"] = "原步行或公交路线耗时较长，已核实改用较快的驾车路线。"
    result["alternatives"] = [{key: alternative[key] for key in ("mode", "duration_s", "distance_m") if key in alternative}]
    return result


def _is_stop(block: dict) -> bool:
    # 去程/回程的航班车次、多选一的餐饮推荐无法定位到一个点
    return bool(block.get("name")) and block.get("type") != "交通" and block.get("note") != "餐饮推荐"


def geocode_blocks(blocks: list[dict], city: str) -> None:
    """原地给可定位的 block 写入 lng/lat/poi_id。"""
    if not os.getenv("AMAP_KEY") or not city:
        return
    stops = [b for b in blocks if _is_stop(b)]
    citycode = ""
    unresolved = []
    for block in stops:
        # 餐厅由高德周边搜索给出可靠坐标，不能再用“午餐”标题重新定位。
        if block.get("options") and (block.get("lng") is None or block.get("lat") is None):
            selected = block.get("selected_option")
            options = block["options"]
            chosen = options[selected] if isinstance(selected, int) and 0 <= selected < len(options) else next((option for option in options if option.get("name") == selected), options[0])
            if chosen.get("lng") is not None and chosen.get("lat") is not None:
                block.update({"lng": chosen["lng"], "lat": chosen["lat"], "selected_option": chosen.get("name")})
        try:
            lng, lat = float(block.get("lng")), float(block.get("lat"))
            located = math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90
        except (TypeError, ValueError):
            located = False
        if located:
            if not citycode:
                citycode = (geocode(city, city) or {}).get("citycode") or ""
            block["_geo"] = {"location": f"{lng},{lat}", "poi_id": block.get("poi_id") or "", "citycode": citycode}
        elif not block.get("options"):
            unresolved.append(block)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        hits = list(ex.map(lambda b: geocode(b["name"], city), unresolved))
    for b, hit in zip(unresolved, hits):
        if hit:
            lng, lat = hit["location"].split(",")
            b["lng"], b["lat"] = float(lng), float(lat)
            b["poi_id"] = hit["poi_id"]
            b["_geo"] = hit
    # 交通块：去程补到达站坐标、返程补出发站坐标，参与「车站↔酒店/景点」路线生成。
    for block in blocks:
        if block.get("type") != "交通" or block.get("_geo"):
            continue
        direction = block.get("direction") or ("去" if block.get("transport_direction") == "去程" else "回")
        station = str(block.get("arr_station") if direction == "去" else block.get("dep_station") or "").strip()
        if not station:
            continue
        hit = geocode(station, city)
        if not hit:
            continue
        lng, lat = hit["location"].split(",")
        block["lng"], block["lat"] = float(lng), float(lat)
        block["poi_id"] = hit.get("poi_id") or ""
        block["_geo"] = {"location": hit["location"], "poi_id": hit.get("poi_id") or "",
                         "citycode": hit.get("citycode") or citycode}
        block["station_name"] = station
    for block in stops:
        if block.get("_geo") and not block.get("link"):
            block["link"] = f"https://www.amap.com/place/{block['poi_id']}" if block.get("poi_id") else (
                f"https://uri.amap.com/marker?position={block['_geo']['location']}&name={urllib.parse.quote(block['name'])}"
            )
    _save_cache()


def attach_routes(result: dict, city: str) -> dict:
    """给 result["blocks"] 补坐标，并生成 result["legs"]：每个方案每天按顺序相邻地点间的路线。

    第 N 天从第 N-1 天的酒店出发，当天最后回到当天酒店。
    """
    blocks = result.get("blocks") or []
    if not os.getenv("AMAP_KEY") or not city or not blocks:
        return result
    geocode_blocks(blocks, city)

    # 按 (方案, 天) 分组，保持 blocks 原有顺序（schedule → 酒店）
    groups: dict[tuple, list[dict]] = {}
    for b in blocks:
        if b.get("_geo"):
            groups.setdefault((b.get("plan_style") or "", b.get("day")), []).append(b)

    pairs: list[tuple[str, int, dict, dict]] = []
    last_hotel: dict[str, dict] = {}
    for (style, day), stops in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        seq = list(stops)
        prev_hotel = last_hotel.get(style)
        if prev_hotel and seq and seq[0]["_geo"]["location"] != prev_hotel["_geo"]["location"]:
            seq.insert(0, prev_hotel)
        for a, b in zip(seq, seq[1:]):
            if a["_geo"]["location"] != b["_geo"]["location"]:
                pairs.append((style, day, a, b))
        hotels = [s for s in stops if s.get("type") == "酒店"]
        if hotels:
            last_hotel[style] = hotels[-1]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        routed = list(ex.map(lambda p: _route_for_schedule(p[2], p[3]), pairs))
    _save_cache()

    legs = []
    for (style, day, a, b), leg in zip(pairs, routed):
        if leg:
            legs.append({"plan_style": style, "day": day, "from": a["id"], "to": b["id"], **leg})
    for b in blocks:
        b.pop("_geo", None)
    result["legs"] = legs
    return result


def strip_geo(blocks: list[dict]) -> None:
    for b in blocks:
        b.pop("_geo", None)
