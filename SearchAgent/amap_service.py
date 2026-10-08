"""高德地图 Web 服务封装：地理编码 + 餐饮 POI 搜索。

返回标准化的餐厅数据，供规划智能体使用：
    name, address, longitude, latitude, cuisine, rating,
    price_per_person, business_area, poi_id, map_url, poi_detail_url

未配置 AMAP_KEY 或请求失败时抛出异常，由调用方决定降级策略。

注意：
- 人均消费 (cost)、评分 (rating)、商圈 (business_area) 在 v5 API 的
  ``poi["business"]`` 对象中，而非旧版 v3 的 ``biz_ext``。
- 高德不直接返回餐厅官网链接，通过 POI ID 和经纬度生成两个高德官方链接：
  地图标记页 https://uri.amap.com/marker?position=lng,lat&name=xxx
  POI 详情页 https://www.amap.com/place/{poi_id}
"""

from __future__ import annotations

import json
import math
import os
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

import certifi

AMAP_BASE = "https://restapi.amap.com"

# 高德 POI 分类编码：050000 = 餐饮服务
FOOD_TYPES = "050000"
BAD_FOOD_KEYWORDS = (
    "宾馆",
    "酒店",
    "旅游景点",
    "公司",
    "休闲场所",
    "美容美发",
    "茶艺馆",
    "茶馆",
    "茶室",
    "茶饮",
    "咖啡",
    "饮品",
    "奶茶",
    "果汁",
    "冷饮",
    "甜品",
    "甜点",
    "冰淇淋",
    "冰激凌",
    "雪糕",
    "糕饼",
    "糕点",
    "蛋糕",
    "面包店",
    "面包房",
    "烘焙",
)


@dataclass
class RestaurantInfo:
    """标准化餐厅信息，规划智能体消费的数据契约。

    rating / price_per_person 缺失时为 None（不是 0），避免下游误判为零分或免费。
    """

    name: str
    address: str
    longitude: float
    latitude: float
    cuisine: str = ""
    rating: float | None = None
    price_per_person: float | None = None
    business_area: str = ""
    poi_id: str = ""
    map_url: str = ""
    poi_detail_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(path: str, **params: Any) -> dict:
    """高德 GET 请求，带 QPS 超限重试。"""
    api_key = os.getenv("AMAP_KEY")
    if not api_key:
        raise RuntimeError("缺少 AMAP_KEY，请在 SearchAgent/.env 中配置高德 Web 服务 Key")
    params["key"] = api_key
    url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(params)}"
    context = ssl.create_default_context(cafile=certifi.where())
    for attempt in range(2):
        with urllib.request.urlopen(url, timeout=10, context=context) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "1":
            return data
        # 10019/10020/10021：QPS 超限，稍等重试一次
        if data.get("infocode") in ("10019", "10020", "10021") and attempt == 0:
            time.sleep(0.5)
            continue
        raise RuntimeError(f"amap {path}: {data.get('info')} ({data.get('infocode')})")
    return {}


def geocode(address: str, city: str | None = None) -> tuple[float, float, str]:
    """地理编码：返回 (经度, 纬度, 标准化地址)。"""
    params: dict[str, Any] = {"address": address}
    if city:
        params["city"] = city
    data = _get("/v3/geocode/geo", **params)
    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise ValueError(f"未找到地址「{address}」")
    loc = geocodes[0].get("location") or ""
    if not loc:
        raise ValueError(f"地址「{address}」无坐标")
    lng_s, lat_s = loc.split(",")
    return float(lng_s), float(lat_s), geocodes[0].get("formatted_address") or address


def _build_map_url(lng: float, lat: float, name: str) -> str:
    """生成高德地图标记页链接（点击可在高德地图中定位餐厅）。"""
    position = f"{lng},{lat}"
    return (
        f"https://uri.amap.com/marker?position={position}"
        f"&name={urllib.parse.quote(name)}"
    )


def _build_poi_detail_url(poi_id: str) -> str:
    """生成高德 POI 详情页链接（餐厅详情、评价、导航入口）。"""
    return f"https://www.amap.com/place/{poi_id}" if poi_id else ""


def _parse_cuisine(type_str: str) -> str:
    """从高德 type 字段（如 '餐饮服务;中餐厅;火锅店'）提取菜系。"""
    if not type_str:
        return ""
    parts = [p for p in type_str.split(";") if p]
    # 取最后一级（最具体的分类），去掉"服务""相关场所"等通用词
    for part in reversed(parts):
        cleaned = (
            part.replace("服务", "")
            .replace("相关场所", "")
            .replace("相关", "")
        )
        if cleaned and cleaned not in ("餐饮",):
            return cleaned
    return parts[-1] if parts else ""


def search_restaurants(
    city: str,
    keyword: str | None = None,
    limit: int = 20,
    location: str | None = None,
    radius: int = 3000,
) -> list[RestaurantInfo]:
    """搜索城市或指定坐标周边的餐厅 POI。

    Args:
        city: 城市名，如 "杭州"
        keyword: 可选关键词，如 "火锅"、"日料"
        limit: 返回数量上限（高德单页最多 25）
        location: 可选中心坐标，格式为 "经度,纬度"；提供时执行周边搜索
        radius: 周边搜索半径，单位米

    Returns:
        标准化餐厅列表，优先返回评分高且有价格信息的餐厅。
    """
    results: list[RestaurantInfo] = []
    seen_names: set[str] = set()
    page_num = 1
    page_size = min(25, max(1, limit))
    first_error: Exception | None = None

    # 过滤咖啡/茶馆或上游重复页时，避免无限翻页拖住餐点搜索。
    max_pages = min(8, max(2, math.ceil(max(1, limit) / 25) + 2))
    while len(results) < limit and page_num <= max_pages:
        params: dict[str, Any] = {
            "types": FOOD_TYPES,
            "page_size": page_size,
            "page_num": page_num,
            "show_fields": "business",
        }
        if location:
            params.update({
                "location": location,
                "radius": max(500, min(radius, 5000)),
                "sortrule": "distance",
            })
        else:
            params.update({"region": city, "city_limit": "true"})
        if keyword:
            params["keywords"] = keyword
        try:
            data = _get("/v5/place/around" if location else "/v5/place/text", **params)
        except Exception as exc:  # noqa: BLE001
            if first_error is None:
                first_error = exc
            break
        pois = data.get("pois") or []
        if not pois:
            break
        raw_count = len(pois)

        for poi in pois:
            poi_location = poi.get("location") or ""
            if not poi_location or "," not in poi_location:
                continue
            lng_s, lat_s = poi_location.split(",")
            try:
                lng = float(lng_s)
                lat = float(lat_s)
            except ValueError:
                continue
            if not (-180 <= lng <= 180 and -90 <= lat <= 90) or (lng == 0 and lat == 0):
                continue

            name = poi.get("name") or ""
            if not name or name in seen_names:
                continue
            seen_names.add(name)

            cuisine = _parse_cuisine(poi.get("type") or "")
            if any(keyword in cuisine for keyword in BAD_FOOD_KEYWORDS):
                continue

            # v5 API：评分、人均、商圈都在 business 对象中
            business = poi.get("business") or {}
            rating_raw = business.get("rating")
            cost_raw = business.get("cost")
            def positive_number(value: Any) -> float | None:
                try:
                    number = float(value)
                    return number if math.isfinite(number) and number > 0 else None
                except (TypeError, ValueError):
                    return None

            rating = positive_number(rating_raw)
            price_per_person = positive_number(cost_raw)
            business_area = business.get("business_area") or poi.get("business_area") or ""

            poi_id = poi.get("id") or ""
            results.append(
                RestaurantInfo(
                    name=name,
                    address=poi.get("address") or "",
                    longitude=lng,
                    latitude=lat,
                    cuisine=cuisine,
                    rating=rating,
                    price_per_person=price_per_person,
                    business_area=business_area,
                    poi_id=poi_id,
                    map_url=_build_map_url(lng, lat, name),
                    poi_detail_url=_build_poi_detail_url(poi_id),
                )
            )
            if len(results) >= limit:
                break

        if raw_count < page_size:
            break
        page_num += 1

    if not results and first_error is not None:
        raise first_error

    # 排序：有评分的优先，评分高的在前；评分相同则人均适中(≈100元)的靠前
    results.sort(
        key=lambda r: (
            1 if r.rating else 0,
            r.rating or 0,
        ),
        reverse=True,
    )
    return results


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

    if len(sys.argv) < 2:
        print("用法: python amap_service.py <城市> [关键词]")
        sys.exit(1)
    city_arg = sys.argv[1]
    kw = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        restaurants = search_restaurants(city_arg, kw)
        print(json.dumps([r.to_dict() for r in restaurants], ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
