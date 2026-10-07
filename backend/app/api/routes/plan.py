import asyncio
import copy
import json
import os
import re
import signal
import importlib.util
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from shared.audit import failed_audit, history_entry, next_plan_revision, normalize_audit, without_review
from shared.sources import merge_plan_sources

ORCHESTRATOR_PY = ROOT / "Orchestrator" / "orchestrator.py"
ORCHESTRATOR_PYTHON = ROOT / "Orchestrator" / ".venv" / "bin" / "python"
SEARCH_PY = ROOT / "SearchAgent" / "search.py"
SEARCH_PYTHON = ROOT / "SearchAgent" / ".venv" / "bin" / "python"
PLAN_PY = ROOT / "PlanAgent" / "plan.py"
PLAN_PYTHON = ROOT / "PlanAgent" / ".venv" / "bin" / "python"
VALIDATE_PY = ROOT / "ValidateAgent" / "validate.py"
VALIDATE_PYTHON = ROOT / "ValidateAgent" / ".venv" / "bin" / "python"

router = APIRouter(prefix="/plan", tags=["plan"])


def _subprocess_env() -> dict:
    """避免 macOS 启动器变量污染子进程的 venv 解析。"""
    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)
    return env


class PlanRequest(BaseModel):
    user_id: str | None = None
    query: str | None = None
    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    profile: dict | None = None
    preferences: dict | None = None
    recent_trips: list | None = None
    basic: dict | None = None
    plan: dict | None = None
    search: dict | None = None
    modify: dict | None = None


def _run_json(python: Path, script: Path, data: dict | str, timeout: int = 120, args: tuple = ()) -> dict:
    try:
        proc = subprocess.run(
            [str(python), str(script), *args],
            input=data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=_subprocess_env(),
            timeout=timeout,
        )
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            return {"error": "服务返回了无效结果，请重试"}
        if getattr(proc, "returncode", 0) and "error" not in result:
            return {"error": "规划服务执行失败，请重试"}
        return result
    except subprocess.TimeoutExpired:
        return {"error": "规划处理超时，请缩小调整范围后重试"}
    except (json.JSONDecodeError, OSError):
        return {"error": "规划服务暂时不可用，请重试"}


def _parse_query(query: str) -> dict:
    return _run_json(SEARCH_PYTHON, SEARCH_PY, query, args=("--parse",))


def _classify_modify(instruction: str, blocks: list) -> dict:
    return _run_json(PLAN_PYTHON, PLAN_PY, {"classify": True, "instruction": instruction, "blocks": blocks})


def _request_context(payload: PlanRequest) -> tuple[str, str, str, dict]:
    destination, start_date, end_date = payload.destination, payload.start_date, payload.end_date
    parsed: dict = {}
    if payload.query and (not destination or not start_date):
        parsed = _parse_query(payload.query)
        destination = destination or parsed.get("destination")
        start_date = start_date or parsed.get("start_date")
        end_date = end_date or parsed.get("end_date")
    if not destination or not destination.strip() or not start_date:
        raise HTTPException(status_code=422, detail="请先补充目的地和出发日期")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date) if end_date else start
        if end < start:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="行程日期无效，结束日期不能早于出发日期") from None
    previous_basic = (payload.plan or {}).get("basic")
    basic = {**(previous_basic if isinstance(previous_basic, dict) else {}), **(payload.basic or {})}
    for source, target in (("origin", "origin"), ("travelers", "travelers"), ("budget", "total_budget"), ("purposes", "purposes")):
        if parsed.get(source) and not basic.get(target):
            basic[target] = parsed[source]
    return destination.strip(), start.isoformat(), end.isoformat(), basic


def _plan_data(payload: PlanRequest, destination: str, start_date: str, end_date: str, basic: dict) -> dict:
    data: dict = {"destination": destination, "start_date": start_date, "end_date": end_date}
    for name in ("user_id", "profile", "preferences", "recent_trips"):
        value = getattr(payload, name)
        if value:
            data[name] = value
    if basic:
        data["basic"] = basic
    return data


def _blocks_to_plan(blocks: list, destination: str, start_date: str, end_date: str, metadata: dict | None = None) -> dict:
    """保留方案、日期、坐标、餐厅选项等信息，重建 PlanAgent 的嵌套日程。"""
    styles: dict[str, dict[int, list[dict]]] = {}
    for block in blocks:
        style = str(block.get("plan_style") or "推荐方案")
        day = int(block.get("day") or 1)
        item = copy.deepcopy(block)
        for key in ("plan_style", "day", "date"):
            item.pop(key, None)
        styles.setdefault(style, {}).setdefault(day, []).append(item)
    summaries = dict((metadata or {}).get("summaries") or {})
    for original in (metadata or {}).get("plans") or []:
        if original.get("style"):
            summaries[original["style"]] = original.get("summary") or ""
    plans = []
    for style, by_day in styles.items():
        itinerary = []
        for day in sorted(by_day):
            schedule = by_day[day]
            date_ = next((str(b["date"]) for b in blocks if b.get("date") and int(b.get("day") or 1) == day
                          and str(b.get("plan_style") or "推荐方案") == style), "")
            if not date_ and start_date:
                date_ = (date.fromisoformat(start_date) + timedelta(days=day - 1)).isoformat()
            window = _day_activity_window(schedule)
            itinerary.append({"day": day, "date": date_, "theme": "", "hotel": "", "schedule": schedule,
                              **({"activity_window": window} if window is not None else {})})
        plans.append({"style": style, "summary": summaries.get(style, ""), "itinerary": itinerary})
    return {**without_review(metadata), "destination": destination, "start_date": start_date, "end_date": end_date, "plans": plans, "blocks": copy.deepcopy(blocks)}


def _search_context(destination: str, start_date: str, end_date: str, basic: dict | None, profile: dict | None) -> dict:
    data = {"destination": destination, "start_date": start_date, "end_date": end_date, "include_food": False}
    if basic:
        data["basic"] = basic
        if basic.get("origin"):
            data["origin"] = basic["origin"]
    if profile:
        data["profile"] = profile
    result = _run_json(SEARCH_PYTHON, SEARCH_PY, data)
    return result if "error" not in result else {"destination": destination}


def _local_modify(destination: str, start_date: str, end_date: str, blocks: list, instruction: str, profile: dict | None, basic: dict | None, full_blocks: list | None = None, modify: dict | None = None, search_result: dict | None = None) -> dict:
    if search_result is None:
        search_result = _search_context(destination, start_date, end_date, basic, profile)
    return _run_json(PLAN_PYTHON, PLAN_PY, {
        "blocks": blocks,
        "full_blocks": full_blocks if full_blocks is not None else blocks,
        "instruction": instruction,
        "modify": modify or {},
        "search": search_result,
        "profile": profile,
        "basic": basic,
    })


def _merge_modified_blocks(result: dict, full_blocks: list, target_ids: set[str] | None = None) -> dict:
    """保留未选择的块以及模型返回的附加数据；局部结果按稳定 id 合并。"""
    if result.get("error"):
        return result
    changed = result.get("blocks")
    if not isinstance(changed, list):
        return {"error": "调整没有返回有效行程，请重试"}
    known = {b["id"] for b in full_blocks}
    if any(not isinstance(b, dict) or b.get("id") not in known for b in changed):
        return {"error": "调整结果无法对应到原计划，请重试"}
    by_id = {b["id"]: b for b in changed if target_ids is None or b["id"] in target_ids}
    merged = []
    for old in full_blocks:
        new = copy.deepcopy(by_id.get(old["id"], old))
        if old["id"] in by_id:
            # 模型只改文字时保留稳定上下文；换地点时不可沿用原坐标或详情链接。
            location_changed = old.get("name") != new.get("name")
            for key in ("plan_style", "day", "date", "time", "type"):
                new.setdefault(key, old.get(key))
            if location_changed:
                for key in ("lng", "lat", "poi_id", "_geo", "link"):
                    if new.get(key) == old.get(key):
                        new.pop(key, None)
            else:
                for key in ("lng", "lat", "poi_id", "link", "price", "options"):
                    if key not in new and key in old:
                        new[key] = copy.deepcopy(old[key])
        merged.append(new)
    return {**result, "blocks": merged}


def _review_modified_plan(plan: dict, search: dict, basic: dict, profile: dict | None,
                          preferences: dict | None = None, recent_trips: list | None = None) -> dict:
    """Review the final snapshot once, preserving an explicit user edit even if blocked."""
    result = without_review(plan)
    result["search"] = merge_plan_sources(search, result)
    reviewed = {key: value for key, value in result.items() if key not in ("search", "review_context")}
    raw = _run_json(VALIDATE_PYTHON, VALIDATE_PY, {
        "plan": reviewed, "search": result["search"], "basic": basic, "profile": profile,
        "preferences": preferences, "recent_trips": recent_trips,
    }, timeout=110)
    try:
        # Do not re-label an old response as a review of this new version.
        if type(raw.get("plan_revision")) is not int or raw["plan_revision"] != result["revision"]:
            raise ValueError("review version mismatch")
        audit = normalize_audit(raw, reviewed, source="mixed")
    except (AttributeError, ValueError, TypeError):
        audit = failed_audit("修改后的审核未能完成", "修改已保留，当前版本需要重新审核；旧审核结论已失效。", reviewed)
    result.update(audit=audit, passed=audit["passed"], history=[history_entry(1, audit)],
                  review_context={"profile": profile, "preferences": preferences, "recent_trips": recent_trips})
    return result


def _finalize_blocks(blocks: list, destination: str, start_date: str, end_date: str, basic: dict,
                     profile: dict | None, refresh_food: bool = False, search_result: dict | None = None,
                     metadata: dict | None = None, refresh_targets: list | None = None,
                     preferences: dict | None = None, recent_trips: list | None = None) -> dict:
    revision = next_plan_revision(metadata)
    draft = _blocks_to_plan(blocks, destination, start_date, end_date, metadata)
    draft.update(revision=revision, basic=copy.deepcopy(basic))
    result = _run_json(PLAN_PYTHON, PLAN_PY, {
        "finalize": True, "plan": draft,
        "search": search_result or {"destination": destination},
        "basic": basic, "profile": profile,
        "refresh_food": refresh_food, "refresh_food_targets": refresh_targets or [],
    }, timeout=300)
    if result.get("error"):
        return {"error": result["error"]}
    if not isinstance(result.get("blocks"), list):
        return {"error": "调整没有返回有效行程，请重试"}
    result = without_review(result)
    result.update(revision=revision, basic=copy.deepcopy(basic))
    snapshot = {**(search_result or {}), "destination": destination,
                "start_date": start_date, "end_date": end_date}
    return _review_modified_plan(result, snapshot, basic, profile, preferences, recent_trips)


def _validated_blocks(value, allow_empty: bool = False) -> list[dict]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 500:
        raise HTTPException(status_code=422, detail="请提供需要调整的完整行程")
    if any(not isinstance(b, dict) or not isinstance(b.get("id"), str) or not b["id"] for b in value):
        raise HTTPException(status_code=422, detail="每条计划需要有效 id")
    if len({b["id"] for b in value}) != len(value):
        raise HTTPException(status_code=422, detail="计划 id 重复，请刷新行程后重试")
    for block in value:
        try:
            day = int(block.get("day") or 1)
            if day < 1 or day > 60:
                raise ValueError
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="计划天数无效") from None
    return copy.deepcopy(value)


def _select_targets(blocks: list[dict], modify: dict) -> list[dict]:
    ids = modify.get("block_ids")
    if ids is not None:
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise HTTPException(status_code=422, detail="所选计划 id 无效")
        known = {b["id"] for b in blocks}
        if set(ids) - known:
            raise HTTPException(status_code=422, detail="所选计划已失效，请重新选择")
        if ids:
            return [b for b in blocks if b["id"] in ids]
    targets = modify.get("targets") or []
    if not isinstance(targets, list):
        raise HTTPException(status_code=422, detail="计划修改目标无效")
    selected = []
    for block in blocks:
        for target in targets:
            if isinstance(target, str):
                matches = target in (block["id"], block.get("name"))
            elif isinstance(target, dict):
                if target.get("id"):
                    matches = target["id"] == block["id"]
                else:
                    matches = bool(target.get("name")) and target["name"] == block.get("name")
                    for key in ("type", "day", "plan_style"):
                        if target.get(key) is not None:
                            matches = matches and str(target[key]) == str(block.get(key))
            else:
                matches = False
            if matches:
                selected.append(block)
                break
    if targets and not selected:
        raise HTTPException(status_code=422, detail="没有找到要修改的计划，请在列表中选择后重试")
    return selected


def _is_delete_instruction(instruction: str) -> bool:
    return not re.search(r"替换|换成|换为|改成|改为|更换", instruction) and bool(re.search(r"删除|删掉|移除|去掉|取消|不去|不要去|不要这|不要该|不要这个|不要那个", instruction))


_ITEM_FIELDS = {"type", "time", "name", "note", "link", "lng", "lat", "poi_id", "price", "options", "selected_option", "rating", "distance_m", "cuisine", "address", "source_option_id", "walking_distance_m", "walking_duration_s", "walking_origin"}


def _candidate_item(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
        raise HTTPException(status_code=422, detail="候选项需要名称")
    item = {key: copy.deepcopy(val) for key, val in value.items() if key in _ITEM_FIELDS}
    item["name"] = item["name"].strip()
    for key, maximum in (("lng", 180), ("lat", 90)):
        if item.get(key) is not None:
            try:
                coord = float(item[key])
                if not -maximum <= coord <= maximum:
                    raise ValueError
                item[key] = coord
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="候选项坐标无效") from None
    return item


def _time_span(value, strict: bool = False) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-—~至]\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if match:
        hours1, minutes1, hours2, minutes2 = map(int, match.groups())
        start, end = hours1 * 60 + minutes1, hours2 * 60 + minutes2
        if 0 <= hours1 < 24 and 0 <= minutes1 < 60 and 0 <= hours2 <= 24 and 0 <= minutes2 < 60 and (hours2 < 24 or minutes2 == 0) and start < end <= 1440:
            return start, end
    if strict:
        raise HTTPException(status_code=422, detail="请填写有效时间段，例如12:00-13:00，结束时间不能超过24:00")
    return None


def _day_activity_window(blocks: list[dict]) -> dict | None:
    windows = []
    for block in blocks:
        window = block.get("activity_window")
        if not isinstance(window, dict):
            continue
        try:
            start, end = int(window["start_min"]), int(window["end_min"])
        except (KeyError, TypeError, ValueError):
            continue
        windows.append({**window, "start_min": max(0, start), "end_min": min(1440, end)})
    if not windows:
        return None
    return {**windows[0], "start_min": max(window["start_min"] for window in windows),
            "end_min": min(window["end_min"] for window in windows)}


def _meal_for_time(value) -> str:
    span = _time_span(value)
    if not span:
        return ""
    return "早餐" if span[0] < 11 * 60 else "午餐" if span[0] < 16 * 60 else "晚餐"


def _check_stop_time(item: dict, day_blocks: list[dict], excluded_id: str | None = None) -> None:
    if item.get("type") == "酒店":
        if item.get("time") and item["time"] != "住宿":
            _time_span(item["time"], strict=True)
        return
    start, end = _time_span(item.get("time"), strict=True)
    window = _day_activity_window(day_blocks)
    if window and not window["start_min"] <= start < end <= window["end_min"]:
        raise HTTPException(status_code=422, detail="这个时间不在当天抵达后、返程前的活动范围内，请换一个时间或日期")
    for block in day_blocks:
        if block.get("type") == "酒店" or block.get("id") == excluded_id:
            continue
        occupied = _time_span(block.get("time"))
        if occupied and start < occupied[1] and end > occupied[0]:
            raise HTTPException(status_code=422, detail="这个时间与已有计划冲突，请换一个时间、日期，或选择要替换的条目")


def _new_block(blocks: list[dict], modify: dict, start_date: str, end_date: str) -> tuple[dict, int]:
    item = _candidate_item(modify.get("item"))
    styles = list(dict.fromkeys(str(b.get("plan_style") or "推荐方案") for b in blocks))
    style = str(modify.get("plan_style") or (styles[0] if styles else "推荐方案"))
    if styles and style not in styles:
        raise HTTPException(status_code=422, detail="所选方案已失效，请重新选择")
    try:
        day = int(modify.get("day") or (modify.get("item") or {}).get("day") or 1)
        max_day = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
        if not 1 <= day <= max_day:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="请选择行程范围内的一天") from None
    day_blocks = [(index, b) for index, b in enumerate(blocks) if str(b.get("plan_style") or "推荐方案") == style and int(b.get("day") or 1) == day]
    day_items = [block for _, block in day_blocks]
    window = _day_activity_window(day_items)
    if window is not None:
        item["activity_window"] = window
    item.setdefault("type", "景点")
    replaces_existing = item.get("type") in ("美食", "酒店") and any(block.get("type") == item["type"] for block in day_items)
    if item.get("time") and not replaces_existing:
        _check_stop_time(item, day_items)
    elif not item.get("time") and item.get("type") == "酒店" and not replaces_existing:
        item["time"] = "住宿"
    elif not item.get("time") and not replaces_existing:
        # 优先安排在当天已有活动间的空档，避免与午餐、晚餐重叠。
        occupied = []
        for _, block in day_blocks:
            if block.get("type") == "酒店":
                continue
            span = _time_span(block.get("time"))
            if span:
                occupied.append(span)
        start = window["start_min"] if window else 9 * 60
        finish_limit = window["end_min"] if window else 20 * 60
        cursor = max(start, 12 * 60 if item.get("type") == "美食" else 9 * 60)
        for left, right in sorted(occupied):
            if right <= cursor:
                continue
            if left - cursor >= 60:
                break
            cursor = max(cursor, right)
        finish = cursor + 60
        if finish > finish_limit:
            raise HTTPException(status_code=422, detail="当天已没有足够空档，请换一天，或选择需要替换的计划条目")
        item["time"] = f"{cursor // 60:02d}:{cursor % 60:02d}-{finish // 60:02d}:{finish % 60:02d}"
    if item.get("type") == "美食" and item.get("time"):
        item["meal"] = _meal_for_time(item["time"])
    item.update({"id": f"b-{uuid4().hex[:12]}", "plan_style": style, "day": day, "date": (date.fromisoformat(start_date) + timedelta(days=day - 1)).isoformat()})
    item.setdefault("note", "已加入行程")
    if item.get("type") in ("美食", "酒店"):
        item["user_selected"] = True
    # 将新活动放进同一天的时间顺序；酒店仍放在当天末尾。
    insert_at = day_blocks[-1][0] + 1 if day_blocks else len(blocks)
    for index, block in day_blocks:
        if block.get("type") == "酒店" or (item.get("time") and block.get("time") and str(block["time"]) > str(item["time"])):
            insert_at = index
            break
    return item, insert_at


def _replace_existing_stop(blocks: list[dict], item: dict, selected: list[dict]) -> dict | None:
    """候选餐厅或酒店优先填入既有时段，避免一天出现重复住宿或餐点。"""
    kind = item.get("type")
    if kind not in ("美食", "酒店"):
        return None
    candidates = [b for b in blocks if b.get("type") == kind
        and str(b.get("plan_style") or "推荐方案") == item["plan_style"]
        and int(b.get("day") or 1) == item["day"]]
    if not candidates:
        return None
    selected_ids = {b["id"] for b in selected}
    explicit = [b for b in candidates if b["id"] in selected_ids]
    target = explicit[0] if explicit else candidates[0]
    explicit_time = bool(item.get("time"))
    if not explicit and kind == "美食" and explicit_time:
        start, _ = _time_span(item["time"], strict=True)
        target = min(candidates, key=lambda block: abs((_time_span(block.get("time")) or (1440, 1440))[0] - start))
    elif not explicit and kind == "美食" and item.get("lng") is not None and item.get("lat") is not None:
        def distance(block):
            if block.get("lng") is None or block.get("lat") is None:
                return float("inf")
            try:
                return (float(block["lng"]) - item["lng"]) ** 2 + (float(block["lat"]) - item["lat"]) ** 2
            except (ValueError, TypeError):
                return float("inf")
        target = min(candidates, key=distance)
    if explicit_time:
        same_day = [b for b in blocks if str(b.get("plan_style") or "推荐方案") == item["plan_style"] and int(b.get("day") or 1) == item["day"]]
        _check_stop_time(item, same_day, target.get("id"))
    if kind == "美食":
        options = copy.deepcopy(target.get("options") or [])
        if not isinstance(options, list):
            options = []
        options = [option for option in options if isinstance(option, dict)]
        option = {key: copy.deepcopy(item[key]) for key in ("name", "price", "link", "lng", "lat", "poi_id", "rating", "distance_m", "cuisine", "address", "walking_distance_m", "walking_duration_s", "walking_origin") if key in item}
        option_index = next((i for i, existing in enumerate(options) if existing.get("name") == option["name"]), len(options))
        if option_index == len(options):
            options.append(option)
        else:
            options[option_index] = option
        meal = _meal_for_time(item["time"]) if explicit_time else target.get("meal") or _meal_for_time(target.get("time"))
        target["options"] = options
        target["selected_option"] = option["name"]
        if meal:
            target["meal"] = meal
    if target.get("name") != item["name"]:
        for key in ("price", "unit_price", "price_basis", "price_known", "price_source"):
            target.pop(key, None)
    elif "price" in item:
        for key in ("unit_price", "price_basis", "price_known", "price_source"):
            target.pop(key, None)
    for key in ("lng", "lat", "poi_id", "link", "_geo", "walking_distance_m", "walking_duration_s", "walking_origin"):
        target.pop(key, None)
    target.update({key: copy.deepcopy(value) for key, value in item.items()
        if key not in ("id", "plan_style", "day", "date", "options", "selected_option")})
    target["user_selected"] = True
    if explicit_time:
        indices = [index for index, block in enumerate(blocks) if str(block.get("plan_style") or "推荐方案") == item["plan_style"] and int(block.get("day") or 1) == item["day"]]
        ordered = sorted((blocks[index] for index in indices), key=lambda block: (block.get("type") == "酒店", (_time_span(block.get("time")) or (1440, 1440))[0]))
        for index, block in zip(indices, ordered):
            blocks[index] = block
    return target


async def _stop_process_group(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if proc.returncode is None:
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if proc.returncode is None:
                proc.kill()
        await proc.wait()


@router.post("/stream")
def plan_stream(payload: PlanRequest):
    """断开连接时立即清理 Orchestrator 及其模型/搜索子进程。"""
    destination, start_date, end_date, basic = _request_context(payload)
    data = _plan_data(payload, destination, start_date, end_date, basic)

    async def event_stream():
        proc = None
        stderr_task = None
        try:
            proc = await asyncio.create_subprocess_exec(
                str(ORCHESTRATOR_PYTHON), str(ORCHESTRATOR_PY), "--stream",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_subprocess_env(), start_new_session=True, limit=4 * 1024 * 1024,
            )
            # 并发排空 stderr，避免日志写满管道导致整个规划停住。
            async def drain_stderr():
                while await proc.stderr.read(8192):
                    pass
            stderr_task = asyncio.create_task(drain_stderr())
            proc.stdin.write(json.dumps(data, ensure_ascii=False).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
                if not line:
                    break
                line = line.decode("utf-8").rstrip("\r\n")
                if line:
                    yield f"data: {line}\n\n"
            await proc.wait()
            if proc.returncode:
                yield f"data: {json.dumps({'type': 'error', 'error': '规划服务执行失败，请重试'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except (asyncio.TimeoutError, OSError, ValueError) as exc:
            message = "规划处理超时，请重试" if isinstance(exc, asyncio.TimeoutError) else "规划服务暂时不可用，请重试"
            yield f"data: {json.dumps({'type': 'error', 'error': message}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            if proc is not None:
                await _stop_process_group(proc)
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


def _global_trip_context(instruction: str, basic: dict, start_date: str, end_date: str) -> dict:
    # Shared helper is pure Python; loading it does not import the model SDK.
    spec = importlib.util.spec_from_file_location("travel_trip_changes", ROOT / "PlanAgent" / "trip_changes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.resolve_trip_changes(instruction, basic, start_date, end_date)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _modify_plan(destination: str, start_date: str, end_date: str, plan: dict, modify: dict, profile: dict | None, basic: dict | None, search_result: dict | None = None) -> dict:
    if search_result is None:
        search_result = _search_context(destination, start_date, end_date, basic, profile)
    return _run_json(PLAN_PYTHON, PLAN_PY, {"plan": plan, "modify": modify, "search": search_result, "profile": profile, "basic": basic, "defer_finalize": True}, timeout=300)


def _affected_days(blocks: list[dict]) -> list[dict]:
    return [{"plan_style": style, "day": day} for style, day in dict.fromkeys((str(b.get("plan_style") or "推荐方案"), int(b.get("day") or 1)) for b in blocks)]


def _merge_style_blocks(original: list[dict], replacement: list[dict], style: str) -> list[dict]:
    untouched_ids = {b["id"] for b in original if str(b.get("plan_style") or "推荐方案") != style}
    normalized = []
    used_ids = set(untouched_ids)
    for block in replacement:
        item = copy.deepcopy(block)
        item["plan_style"] = style
        if not item.get("id") or item["id"] in used_ids:
            item["id"] = f"b-{uuid4().hex[:12]}"
        used_ids.add(item["id"])
        normalized.append(item)
    merged = []
    inserted = False
    for block in original:
        if str(block.get("plan_style") or "推荐方案") == style:
            if not inserted:
                merged.extend(normalized)
                inserted = True
        else:
            merged.append(block)
    return merged


@router.post("")
def create_plan(payload: PlanRequest) -> dict:
    destination, start_date, end_date, basic = _request_context(payload)
    if not payload.modify:
        return _run_json(ORCHESTRATOR_PYTHON, ORCHESTRATOR_PY, _plan_data(payload, destination, start_date, end_date, basic), timeout=600)

    modify = copy.deepcopy(payload.modify)
    action = modify.get("action") or "modify"
    block_input = modify.get("blocks") if "blocks" in modify else (payload.plan or {}).get("blocks")
    blocks = _validated_blocks(block_input, allow_empty=action == "add")
    if action not in ("modify", "add", "delete", "update"):
        raise HTTPException(status_code=422, detail="不支持的计划操作")
    style = modify.get("plan_style")
    scoped = blocks if not style else [b for b in blocks if str(b.get("plan_style") or "推荐方案") == style]
    if not scoped and not (action == "add" and not blocks):
        raise HTTPException(status_code=422, detail="所选方案已失效，请重新选择")

    def finalize(updated: list, affected: list, refresh_food: bool = False, search: dict | None = None, metadata: dict | None = None):
        return _finalize_blocks(updated, destination, start_date, end_date, basic, payload.profile,
            refresh_food=refresh_food, search_result=search if search is not None else payload.search,
            metadata=metadata or payload.plan, refresh_targets=_affected_days(affected),
            preferences=payload.preferences, recent_trips=payload.recent_trips)

    if action == "add":
        selected = _select_targets(scoped, modify)
        item, index = _new_block(blocks, modify, start_date, end_date)
        replaced = _replace_existing_stop(blocks, item, selected)
        if replaced is not None:
            return finalize(blocks, [replaced], refresh_food=False)
        blocks.insert(index, item)
        return finalize(blocks, [item], refresh_food=item.get("type") != "美食")

    selected = _select_targets(scoped, modify)
    instruction = str(modify.get("instruction") or "").strip()
    if action == "update":
        if len(selected) != 1:
            raise HTTPException(status_code=422, detail="请选择一条计划进行更新")
        item = _candidate_item(modify.get("item"))
        target = selected[0]
        if target.get("name") != item["name"]:
            for key in ("price", "unit_price", "price_basis", "price_known", "price_source"):
                target.pop(key, None)
        elif "price" in item:
            target.pop("unit_price", None)
            target.pop("price_basis", None)
            target.pop("price_known", None)
            target.pop("price_source", None)
        # 明确选择一个餐厅时直接保存用户选择，不再让模型替换为其他餐厅。
        if target.get("name") != item["name"]:
            for key in ("lng", "lat", "poi_id", "link", "selected_option", "walking_distance_m", "walking_duration_s", "walking_origin"):
                target.pop(key, None)
        elif any(key in item and item.get(key) != target.get(key) for key in ("lng", "lat")):
            for key in ("walking_distance_m", "walking_duration_s", "walking_origin"):
                target.pop(key, None)
        target.update(item)
        target["user_selected"] = True
        return finalize(blocks, [target], refresh_food=target.get("type") != "美食")

    if action == "modify" and not instruction:
        raise HTTPException(status_code=422, detail="请输入希望如何调整计划")
    if selected:
        modify["mode"] = "block"
        modify["block_ids"] = [block["id"] for block in selected]
    elif not modify.get("mode") and action == "modify":
        classification = _classify_modify(instruction, scoped)
        if classification.get("error"):
            return classification
        modify.update(classification)
        selected = _select_targets(scoped, modify)
    if action == "delete" or (selected and _is_delete_instruction(instruction)):
        if not selected:
            raise HTTPException(status_code=422, detail="请选择需要删除的计划")
        ids = {block["id"] for block in selected}
        remaining = [block for block in blocks if block["id"] not in ids]
        return finalize(remaining, selected, refresh_food=any(block.get("type") != "美食" for block in selected))

    if modify.get("mode") == "global" and not selected:
        trip_context = _global_trip_context(instruction,
            {**((payload.plan or {}).get("basic") or {}), **basic}, start_date, end_date)
        start_date, end_date = trip_context["start_date"], trip_context["end_date"]
        basic = trip_context["basic"]
        search = payload.search
        if search is None or trip_context["updates"].get("dates"):
            search = _search_context(destination, start_date, end_date, basic, payload.profile)
        plan = _blocks_to_plan(scoped, destination, start_date, end_date, payload.plan)
        result = _modify_plan(destination, start_date, end_date, plan,
            {**modify, "instruction": instruction, "_trip_context": trip_context}, payload.profile, basic, search_result=search)
        if result.get("error"):
            return result
        changed = result.get("blocks")
        if not isinstance(changed, list) or any(not isinstance(b, dict) for b in changed):
            return {"error": "调整没有返回有效行程，请重试"}
        merged = _merge_style_blocks(blocks, changed, style) if style else changed
        # Dates apply to the trip, including alternative styles that were not
        # replanned. Drop days beyond a shortened trip and align all calendars.
        merged = [block for block in merged if 1 <= int(block.get("day") or 1) <= trip_context["days"]]
        for block in merged:
            block["date"] = (date.fromisoformat(start_date) + timedelta(days=int(block.get("day") or 1) - 1)).isoformat()
        metadata = {**(payload.plan or {}), "plans": result.get("plans") or plan["plans"],
                    "basic": basic, "start_date": start_date, "end_date": end_date, "days": trip_context["days"]}
        affected = blocks + merged if trip_context["updates"] else scoped + changed
        return finalize(merged, affected, refresh_food=True, search=search, metadata=metadata)
    if not selected:
        return {"error": "请先选择需要调整的计划，或描述要优化的整个方案"}
    ids = {block["id"] for block in selected}
    search = payload.search
    if search is None:
        search = _search_context(destination, start_date, end_date, basic, payload.profile)
    result = _local_modify(destination, start_date, end_date, selected, instruction, payload.profile, basic,
        full_blocks=blocks, modify={**modify, "block_ids": list(ids)}, search_result=search)
    result = _merge_modified_blocks(result, blocks, ids)
    if result.get("error"):
        return result
    return finalize(result["blocks"], selected, refresh_food=any(block.get("type") != "美食" for block in selected), search=search)
