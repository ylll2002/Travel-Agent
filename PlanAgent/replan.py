"""Replace identified slots, then repair their dependent timeline using real routes.

The block list is authoritative for this operation. Model output may only select
catalog candidates; coordinates, prices, IDs and timing are controlled here.
Callbacks keep the constraint checks testable without paid/network services.
"""

from copy import deepcopy
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.pricing import item_price, parse_price
from shared.travel import travel_rows, travel_metadata, is_local_transport, attach_travel_metadata


class ReplanError(ValueError):
    pass


def parse_selection(content, finish_reason=None):
    if finish_reason == "length":
        raise ReplanError("模型输出被截断，请仅返回替换项JSON。")
    if not isinstance(content, str) or not content.strip():
        raise ReplanError("模型返回空内容，请返回replacements列表。")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplanError("模型返回的JSON格式无效，请仅返回替换项JSON。") from exc
    if not isinstance(result, dict):
        raise ReplanError("模型必须返回包含replacements的JSON对象。")
    return result


def selection_context(context):
    """Remove duplicate nested plans and route geometry without losing constraints."""
    compact = deepcopy(context)
    compact["plan"] = {k: v for k, v in compact["plan"].items() if k not in ("plans", "legs")}
    compact["plan"]["blocks"] = [
        {k: v for k, v in block.items() if k not in ("polyline", "geometry")}
        for block in compact["plan"].get("blocks", [])
    ]
    compact["required_block_ids"] = [b["id"] for b in compact["targets"]]
    return compact


def is_stop(block):
    return block.get("type") != "交通" and not (block.get("type") == "酒店" and "无住宿" in block.get("name", ""))


def minutes(value):
    match = re.search(r"(?<!\d)([0-2]?\d):([0-5]\d)", str(value or ""))
    if not match or int(match[1]) > 23:
        return None
    return int(match[1]) * 60 + int(match[2])


def span(block):
    clocks = re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)", str(block.get("time") or ""))
    start = minutes(clocks[0]) if clocks else None
    end = minutes(clocks[1]) if len(clocks) > 1 else None
    return start, end


def clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def amount(value):
    return parse_price(value)


def catalog(search, target):
    kind = target.get("type")
    key = {"景点": "poi", "美食": "food", "酒店": "hotels", "活动": "events"}.get(kind)
    items = (search.get(key) or []) if key else []
    if kind == "交通":
        items = []
        for row in travel_rows(search):
            dep = str(row.get("dep_time") or "")
            arr = str(row.get("arr_time") or "")
            # Cross-midnight travel needs a multi-day timeline; do not guess.
            dep_minutes = minutes(dep)
            arr_minutes = minutes(arr)
            if dep_minutes is None or arr_minutes is None or arr_minutes <= dep_minutes:
                continue
            if target.get("date") and dep[:10] != target["date"]:
                continue
            items.append(row)
    if not isinstance(items, list):
        return []
    result = []
    seen = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("title") or "").strip()
        if not name or name == target.get("name") or name in seen:
            continue
        event_dates = re.findall(r"\d{4}-\d{2}-\d{2}", str(row.get("date") or row.get("content") or ""))
        if kind == "活动" and event_dates and not (event_dates[0] <= target.get("date", "") <= event_dates[-1]):
            continue
        seen.add(name)
        result.append({**row, "name": name, "candidate_id": f"c{len(result)}"})
    return result


def replacement(block, candidate):
    new = deepcopy(block)
    for field in ("lng", "lat", "poi_id", "_geo", "options", "price", "unit_price", "price_basis", "price_known", "price_source", "link", "note", "opening_hours", "selected_option", "source_option_id", "anchor_name", "distance_m", "distance_km", "walking_distance_m", "walking_duration_s", "walking_origin", "rating"):
        new.pop(field, None)
    new["name"] = candidate["name"]
    new["link"] = candidate.get("url") or candidate.get("link") or candidate.get("poi_detail_url") or candidate.get("detail_url") or candidate.get("map_url") or ""
    new["note"] = "根据你的要求重新选择"
    new["price"] = item_price(candidate)
    new["unit_price"] = new["price"]
    new["price_known"] = new["price"] is not None
    new["price_source"] = "search" if new["price_known"] else "unknown"
    new["price_basis"] = candidate.get("price_basis") if candidate.get("price_basis") in ("group", "per_person") else ("group" if block.get("type") == "酒店" else "per_person")
    if block.get("type") == "美食":
        new["selected_option"] = candidate["name"]
    new["opening_hours"] = candidate.get("opening_hours") or candidate.get("open_hours") or candidate.get("openHours") or ""
    if candidate.get("_travel"):
        new["time"] = f"{clock(minutes(candidate['dep_time']))}-{clock(minutes(candidate['arr_time']))}"
        new["note"] = f"{candidate.get('dep_station', '')} → {candidate.get('arr_station', '')}"
        new["fixed_time"] = True
        old_dep, _ = span(block)
        # Replacing the outbound leg shifts later activities from arrival time;
        # return travel remains a deadline for preceding activities.
        new["direction"] = candidate.get("direction") or ("去" if old_dep is not None and old_dep < 12 * 60 else "返")
        new["dep_station"] = candidate.get("dep_station")
        new["arr_station"] = candidate.get("arr_station")
    if block.get("type") == "活动" and minutes(candidate.get("start_time")) is not None:
        start = minutes(candidate["start_time"])
        _, old_end = span(block)
        old_start, _ = span(block)
        start = minutes(candidate.get("start_time")) or 0
        end = minutes(candidate.get("end_time")) or start + ((old_end - old_start) if old_start is not None and old_end is not None else 120)
        new["time"] = f"{clock(start)}-{clock(end)}"
        new["fixed_time"] = True
    return new


def route_stops(blocks, style, search):
    """Include airport/station arrival or departure as the real route boundary."""
    stops = []
    for block in blocks:
        if block.get("plan_style") != style:
            continue
        if block.get("type") != "交通":
            if is_stop(block):
                stops.append(block)
            continue
        if is_local_transport(block, search):
            continue
        row = travel_metadata(block, search)
        start, _ = span(block)
        outbound = (block.get("direction") or row.get("direction")) == "去" or (not block.get("direction") and not row.get("direction") and start is not None and start < 12 * 60)
        name = (block.get("arr_station") or row.get("arr_station")) if outbound else (block.get("dep_station") or row.get("dep_station"))
        if not name:
            raise ReplanError(f"出行项目「{block['name']}」缺少机场或车站信息，无法检查接续交通。请确认该班次的出发站和到达站。")
        stops.append({**block, "name": name, "type": "接续交通", "note": "机场或车站接续点"})
    return stops


def expected_pairs(blocks, style):
    groups = {}
    for block in blocks:
        if block.get("plan_style") == style and is_stop(block):
            if block.get("note") == "餐饮推荐" or not block.get("name"):
                raise ReplanError("请先确定具体餐厅，再重新规划相关行程。")
            groups.setdefault(block["day"], []).append(block)
    pairs = []
    previous_hotel = None
    for day, stops in sorted(groups.items()):
        sequence = ([previous_hotel] if previous_hotel else []) + stops
        for left, right in zip(sequence, sequence[1:]):
            if left.get("name") != right.get("name") and (left.get("lng"), left.get("lat")) != (right.get("lng"), right.get("lat")):
                pairs.append((day, left["id"], right["id"]))
        hotels = [b for b in stops if b.get("type") == "酒店"]
        if hotels:
            previous_hotel = hotels[-1]
    return pairs


def repair_timeline(plan, old_blocks, style, affected_days, target_ids, locked_ids, directions=None):
    route_minutes = {(leg["day"], leg["from"], leg["to"]): math.ceil(float(leg["duration_s"]) / 60) + 10 for leg in plan.get("legs", []) if leg.get("plan_style") == style}
    old_by_id = {b["id"]: b for b in old_blocks}
    all_days = sorted({b["day"] for b in plan["blocks"] if b.get("plan_style") == style})
    previous_hotel = None
    for day in all_days:
        rows = [b for b in plan["blocks"] if b.get("plan_style") == style and b["day"] == day]
        cursor = None
        previous = previous_hotel
        local_transfers = []
        def refresh_local_transfers(next_block, travel):
            if not local_transfers:
                return
            if previous is None or cursor is None:
                raise ReplanError("市内交通缺少前一地点或时间，无法安全调整。")
            count = len(local_transfers)
            for index, transfer in enumerate(local_transfers):
                start = cursor + travel * index // count
                end = cursor + travel * (index + 1) // count
                new_time = f"{clock(start)}-{clock(end)}"
                if (transfer["id"] in locked_ids or transfer.get("fixed_time")) and new_time != transfer.get("time"):
                    raise ReplanError(f"调整会影响固定项目「{transfer['name']}」，请换一个替代项。")
                transfer["time"] = new_time
                transfer["name"] = f"{previous['name']} → {next_block['name']}"
                transfer["note"] = "已按真实路线重新计算市内交通（含10分钟缓冲）"
            local_transfers.clear()
        may_shift = day in affected_days and not any(b["id"] in target_ids for b in rows)
        for block in rows:
            if day not in affected_days or (block.get("type") == "酒店" and not is_stop(block)):
                continue
            start, end = span(block)
            original = old_by_id[block["id"]]
            if block.get("type") == "交通":
                if block.get("transport_scope") == "local":
                    # Real legs between the surrounding stops account for this
                    # transfer. It is not a scheduled flight/train deadline.
                    if may_shift:
                        local_transfers.append(block)
                    continue
                if block["id"] in target_ids:
                    may_shift = True
                if start is None or end is None:
                    raise ReplanError("出行项目缺少明确的出发与到达时间，无法检查后续安排。")
                transfer = route_minutes.get((day, previous["id"], block["id"]), 0) if previous else 0
                refresh_local_transfers(block, transfer)
                if cursor is not None and start < cursor + transfer + 60:
                    raise ReplanError("调整后赶不上固定出行时间（需至少预留一小时），请换一个候选或减少当天活动。")
                direction = block.get("direction") or (directions or {}).get(block["id"])
                if direction == "去" or (not direction and cursor is None and start < 12 * 60):
                    cursor = end + 60
                    previous = block
                continue
            if block["id"] in target_ids:
                may_shift = True
            if not may_shift:
                cursor = end if end is not None else (start + 30 if start is not None else cursor)
                previous = block
                continue
            travel = route_minutes.get((day, previous["id"], block["id"]), 0) if previous else 0
            refresh_local_transfers(block, travel)
            if start is None:
                if block.get("type") != "酒店":
                    raise ReplanError(f"「{block['name']}」缺少明确时间，无法安全调整行程。")
                start = max(cursor + travel if cursor is not None else 18 * 60, 14 * 60)
                if not may_shift:
                    previous = block
                    continue
            required = (cursor + travel) if cursor is not None else start
            # Hotel departure on the following day has a known real route. Leave
            # the planned first start unchanged unless reaching it before 08:00
            # departure would be required.
            if cursor is None and previous_hotel and previous is previous_hotel:
                required = max(required, 8 * 60 + travel)
            window = block.get("activity_window") or {}
            window_start = window.get("start_min") if block.get("type") != "酒店" else None
            window_end = window.get("end_min") if block.get("type") != "酒店" else None
            new_start = max(start, required, window_start if isinstance(window_start, (int, float)) else start)
            if block["id"] in locked_ids or block.get("fixed_time") or not may_shift:
                if new_start > start:
                    raise ReplanError(f"调整会影响固定项目「{block['name']}」，请换一个替代项。")
            else:
                duration = end - start if end is not None and end > start else {"景点": 120, "美食": 60, "酒店": 30, "活动": 120}.get(block.get("type"), 60)
                end = new_start + duration
                block["time"] = f"{clock(new_start)}-{clock(end)}"
                start = new_start
            cursor = end if end is not None else start + 30
            if isinstance(window_end, (int, float)) and cursor > window_end:
                raise ReplanError(f"「{block['name']}」超出当天可活动时间，无法赶上后续安排。")
            if cursor > 23 * 60 + 30:
                raise ReplanError("调整后的行程超过23:30，请选择更近的地点或减少当天活动。")
            hours = str(block.get("opening_hours") or "")
            bounds = re.findall(r"\d{1,2}:\d{2}", hours)
            if len(bounds) == 2:
                opens, closes = minutes(bounds[0]), minutes(bounds[1])
                if opens is not None and closes is not None and closes > opens and (start < opens or cursor > closes):
                    raise ReplanError(f"「{block['name']}」的营业时间与调整后的安排冲突。")
            if block["id"] in locked_ids and block != original:
                raise ReplanError("固定项目不能被更改。")
            previous = block
        if local_transfers:
            raise ReplanError("市内交通缺少后续地点，无法安全调整。")
        hotels = [b for b in rows if b.get("type") == "酒店" and is_stop(b)]
        if hotels:
            previous_hotel = hotels[-1]


def rebuild_itinerary(plan):
    """Derive compatibility views from authoritative blocks without regenerating IDs."""
    plans = []
    for style in dict.fromkeys(b.get("plan_style", "") for b in plan["blocks"]):
        previous = next((p for p in plan.get("plans", []) if p.get("style") == style), {})
        itinerary = []
        days = sorted({b["day"] for b in plan["blocks"] if b.get("plan_style", "") == style})
        for day in days:
            rows = [b for b in plan["blocks"] if b.get("plan_style", "") == style and b["day"] == day]
            prior_day = next((d for d in previous.get("itinerary", []) if d.get("day") == day), {})
            itinerary.append({**{key: value for key, value in prior_day.items() if key not in ("schedule", "hotel", "hotel_link", "meals")}, "day": day, "date": rows[0].get("date", ""), "theme": prior_day.get("theme", ""), "hotel": next((b["name"] for b in rows if b.get("type") == "酒店"), ""), "schedule": deepcopy(rows)})
        plans.append({**previous, "style": style, "summary": previous.get("summary") or plan.get("summaries", {}).get(style, ""), "itinerary": itinerary})
    plan["plans"] = plans
    return plan


def replan_plan(payload, search, choose, attach_routes, refresh_meals=None):
    original = deepcopy(payload["plan"])
    attach_travel_metadata(original.get("blocks") or [], search)
    blocks = original.get("blocks") or []
    ids = [b.get("id") for b in blocks]
    if not blocks or any(not bid for bid in ids) or len(set(ids)) != len(ids):
        raise ReplanError("方案缺少唯一活动ID，请重新生成方案。")
    if payload["revision"] != original.get("revision", 0):
        raise ReplanError("方案版本已变化，请重新选择活动。")
    target_ids = set(payload["target_block_ids"])
    locked_ids = set(payload.get("locked_block_ids") or [])
    style = payload["plan_style"]
    targets = [b for b in blocks if b["id"] in target_ids]
    if not targets or len(targets) != len(target_ids) or any(b.get("plan_style") != style for b in targets):
        raise ReplanError("选中活动不属于当前方案。")
    if locked_ids - set(ids) or target_ids & locked_ids:
        raise ReplanError("选中活动已锁定或锁定ID无效。")
    if any(b.get("type") not in ("景点", "美食", "酒店", "活动", "交通") for b in targets):
        raise ReplanError("该类别不能重新规划。")
    if any(b.get("type") == "酒店" and not is_stop(b) for b in targets):
        raise ReplanError("当晚无住宿，不存在可替换的酒店。")
    rebuild_itinerary(original)
    candidate_map = {b["id"]: catalog(search, b) for b in targets}
    existing_names = {b["name"] for b in blocks if b.get("plan_style") == style and b["id"] not in target_ids}
    for bid, candidates in candidate_map.items():
        candidate_map[bid] = [c for c in candidates if c["name"] not in existing_names or next(b for b in targets if b["id"] == bid).get("type") == "酒店"]
        if not candidate_map[bid]:
            raise ReplanError("没有可用的同类替代项，请调整原因或重新搜索。")
    affected_days = {b["day"] for b in targets}
    feedback = ""
    for attempt in range(3):
        updated = deepcopy(original)
        affected_days = {b["day"] for b in targets}
        changed_ids = set(target_ids)
        try:
            decisions = choose({"plan": original, "targets": targets, "candidates": candidate_map, "instruction": payload["instruction"], "profile": payload.get("profile"), "basic": payload.get("basic"), "feedback": feedback})
            picks = decisions.get("replacements") if isinstance(decisions, dict) else None
            if (not isinstance(picks, list) or len(picks) != len(target_ids)
                    or any(not isinstance(p, dict) or not isinstance(p.get("block_id"), str)
                           or not isinstance(p.get("candidate_id"), str) for p in picks)
                    or {p["block_id"] for p in picks} != target_ids):
                raise ReplanError("模型未返回完整的替换结果：请为每个选中活动返回准确的block_id和candidate_id。")
            for pick in picks:
                candidate = next((c for c in candidate_map[pick["block_id"]] if c["candidate_id"] == pick.get("candidate_id")), None)
                if candidate is None:
                    raise ReplanError("替代项不在搜索结果中。")
                old = next(b for b in blocks if b["id"] == pick["block_id"])
                for i, block in enumerate(updated["blocks"]):
                    same_stay = old["type"] == "酒店" and block.get("plan_style") == style and block.get("type") == "酒店" and block.get("name") == old.get("name")
                    if block["id"] == old["id"] or same_stay:
                        if block["id"] in locked_ids:
                            raise ReplanError("同一住宿包含锁定项目，无法一起替换。")
                        updated["blocks"][i] = replacement(block, candidate)
                        changed_ids.add(block["id"])
                        affected_days.add(block["day"])
                        if old["type"] == "酒店":
                            affected_days.add(block["day"] + 1)
            match = re.search(r"\d+", str((payload.get("basic") or {}).get("travelers") or "1"))
            travelers = max(1, int(match[0])) if match else 1
            for block in updated["blocks"]:
                if block["id"] in changed_ids:
                    unit = amount(block.get("unit_price"))
                    block["price"] = round(unit * (travelers if block.get("price_basis") == "per_person" else 1), 2) if unit is not None else None
            names = [b["name"] for b in updated["blocks"] if b.get("plan_style") == style and b.get("type") == "景点"]
            if len(names) != len(set(names)):
                raise ReplanError("替换后出现重复景点。")
            # Recompute just this style. Other styles keep every block and leg.
            routed = {"blocks": deepcopy(route_stops(updated["blocks"], style, search))}
            attach_routes(routed, original["destination"])
            pairs = expected_pairs(routed["blocks"], style)
            new_legs = routed.get("legs") or []
            known = {(leg["day"], leg["from"], leg["to"]) for leg in new_legs if isinstance(leg.get("duration_s"), (int, float)) and math.isfinite(leg["duration_s"]) and leg["duration_s"] >= 0}
            if set(pairs) - known:
                raise ReplanError("未取得完整的真实交通路线，请检查高德配置后重试。")
            for b in routed["blocks"]:
                if is_stop(b) and (b.get("lng") is None or b.get("lat") is None):
                    raise ReplanError(f"无法定位「{b['name']}」，原方案已保留。")
            routed_by_id = {b["id"]: b for b in routed["blocks"]}
            updated["blocks"] = [routed_by_id.get(b["id"], b) if b.get("plan_style") == style and b["day"] in affected_days and is_stop(b) else b for b in updated["blocks"]]
            updated["legs"] = [leg for leg in original.get("legs", []) if leg.get("plan_style") != style or leg.get("day") not in affected_days] + [leg for leg in new_legs if leg.get("day") in affected_days]
            directions = {b["id"]: travel_metadata(b, search).get("direction") for b in updated["blocks"] if b.get("type") == "交通"}
            repair_timeline(updated, blocks, style, affected_days, changed_ids, locked_ids, directions)
            if refresh_meals:
                refresh_meals({"blocks": [b for b in updated["blocks"] if b.get("plan_style") == style and b["day"] in affected_days]})
            old_by_id = {b["id"]: b for b in blocks}
            if any(b != old_by_id[b["id"]] for b in updated["blocks"] if b["id"] in locked_ids):
                raise ReplanError("固定项目不能被更改。")
            costs = {}
            unknown_prices = []
            unpriced_items = {}
            for b in updated["blocks"]:
                block_style = b.get("plan_style", "")
                unknown = amount(b.get("price")) is None or b.get("price_known") is False or b.get("price_source") == "unknown"
                if unknown:
                    unknown_prices.append(b["id"])
                    unpriced_items.setdefault(block_style, []).append(b["name"])
                costs[block_style] = round(costs.get(block_style, 0) + (0 if unknown else amount(b.get("price")) or 0), 2)
            budget = amount((payload.get("basic") or {}).get("total_budget"))
            if budget and costs[style] > budget:
                raise ReplanError("替换后超出总预算，请选择更便宜的替代项。")
            if budget and any(bid in changed_ids for bid in unknown_prices):
                raise ReplanError("替代项缺少价格，无法确认是否满足预算。")
            updated["cost_by_style"] = costs
            updated["total_cost"] = costs[style]
            updated["unknown_price_block_ids"] = unknown_prices
            updated["budget_by_style"] = {s: "over" if budget and cost > budget else "unknown" if unpriced_items.get(s) else "ok" for s, cost in costs.items()}
            updated["budget_status"] = updated["budget_by_style"][style]
            updated["unpriced_items"] = unpriced_items
            updated["revision"] = payload["revision"] + 1
            rebuild_itinerary(updated)
            changed = [b["id"] for b in updated["blocks"] if b != old_by_id[b["id"]]]
            changes = []
            for b in updated["blocks"]:
                old = old_by_id[b["id"]]
                if old["name"] != b["name"]:
                    changes.append(f"第{b['day']}天：{old['name']}替换为{b['name']}")
                if old.get("time") != b.get("time"):
                    changes.append(f"{b['name']}：时间调整为{b['time']}")
            changes.append("已重新计算相关日期的地图路线和费用")
            return {"revision": updated["revision"], "plan": updated, "changed_block_ids": changed, "affected_days": sorted(affected_days & {b["day"] for b in blocks if b.get("plan_style") == style}), "changes": changes}
        except ReplanError as exc:
            feedback = str(exc)
            if attempt == 2:
                raise ReplanError(f"{feedback} 原方案已保留。") from exc
    raise ReplanError("无法重新规划，原方案已保留。")


def main():
    import json
    import os
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    from openai import OpenAI
    from route_map import attach_routes
    from plan import _refresh_selected_meal_distances

    load_dotenv(Path(__file__).resolve().parent / ".env")
    try:
        data = json.load(sys.stdin)
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL") or None)

        def choose(context):
            # Route geometry adds no useful candidate-selection context.
            context = selection_context(context)
            response = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "deepseek-flash"),
                messages=[
                    {"role": "system", "content": (
                        "根据用户原因、画像和完整行程，为每个选中活动选择同类替代项。"
                        "必须遵守忌口、预算、活动日期和固定项目；只能选择对应candidates中的candidate_id。"
                        "不要修改时间或编造地点。required_block_ids中的每个ID必须恰好出现一次。"
                        "遇到feedback时修正输出或选择可行候选。不要解释，只返回JSON："
                        '{"replacements":[{"block_id":"...","candidate_id":"..."}]}。'
                    )},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                max_tokens=12000 if context.get("feedback") else 6000,
                timeout=120,
            )
            if not response.choices:
                raise ReplanError("模型未返回候选结果，请重试。")
            choice = response.choices[0]
            return parse_selection(choice.message.content, choice.finish_reason)

        output = replan_plan(data, data["search"], choose, attach_routes, _refresh_selected_meal_distances)
    except Exception as exc:
        output = {"error": str(exc)}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
