"""Deterministic, read-only plan checks. Runs with Python's standard library only."""
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.audit import failed_audit, normalize_audit
from shared.route_timing import MODE_LABELS, route_gap_status

SOURCES = {"景点": ("poi",), "酒店": ("hotels",), "美食": ("food",), "餐饮": ("food",),
           "活动": ("events", "poi", "promotions")}
COST_TYPES = set(SOURCES) | {"交通"}
LOCAL_TZ = timezone(timedelta(hours=8))
UNNAMED_ACTIVITIES = {"自由活动", "自由时间", "自由安排", "休息", "候车", "返回酒店", "办理入住"}
MEAL_LABELS = {"早餐", "午餐", "晚餐"}


def _money(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).strip().upper()
        if text in ("免费", "免票"):
            return Decimal(0)
        text = re.sub(r"^(?:¥|RMB|CNY)\s*", "", text)
        text = re.sub(r"\s*元$", "", text)
        if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
            return None
        value = text.replace(",", "")
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _name(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _rows(plan):
    """Flat blocks are authoritative; nested schedules are a fallback, never added twice."""
    blocks = plan.get("blocks")
    if blocks is not None:
        if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
            raise ValueError("blocks必须是活动对象列表")
        rows = [(b, f"plan.blocks[{i}]") for i, b in enumerate(blocks)]
    else:
        rows = []
        if not isinstance(plan.get("plans", []), list):
            raise ValueError("plans必须是列表")
        for pi, style in enumerate(plan.get("plans") or []):
            if not isinstance(style, dict) or not isinstance(style.get("itinerary", []), list):
                raise ValueError("方案结构无效")
            for di, day in enumerate(style.get("itinerary") or []):
                if not isinstance(day, dict) or not isinstance(day.get("schedule", []), list):
                    raise ValueError("日程结构无效")
                for bi, block in enumerate(day.get("schedule") or []):
                    if not isinstance(block, dict):
                        raise ValueError("活动结构无效")
                    rows.append(({**block, "plan_style": style.get("style"), "day": day.get("day"),
                                  "date": day.get("date")}, f"plan.plans[{pi}].itinerary[{di}].schedule[{bi}]"))
                hotel = day.get("hotel")
                if (isinstance(hotel, str) and hotel.strip()
                        and not any(b.get("type") == "酒店" for b in day.get("schedule") or [])
                        and not any(label in hotel for label in ("无住宿", "无需住宿", "不住宿", "当天返程", "当晚返程"))):
                    rows.append(({"name": hotel, "type": "酒店", "plan_style": style.get("style"),
                                  "day": day.get("day"), "date": day.get("date"), "link": day.get("hotel_link")},
                                 f"plan.plans[{pi}].itinerary[{di}]"))
    ids = [b["id"] for b, _ in rows if b.get("id")]
    if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("活动ID无效或重复")
    if len(rows) > 500:
        raise ValueError("活动数量超过500")
    for b, _ in rows:
        if b.get("day") is not None and (type(b["day"]) is not int or b["day"] < 1):
            raise ValueError("活动天数无效")
        if b.get("date") and (_date(b["date"]) is None or _date(b["date"]).isoformat() != b["date"]):
            raise ValueError("活动日期无效")
        if b.get("plan_style") is not None and not isinstance(b["plan_style"], str):
            raise ValueError("方案名称无效")
    return rows


def _evidence(path, value):
    return {"path": path, "value": value}


def _add(issues, kind, severity, detail, suggestion, rows=(), evidence=(), actionable=False):
    blocks = [b for b, _ in rows]
    def common(key):
        values = [b.get(key) for b in blocks]
        return values[0] if values and all(v == values[0] for v in values) else None
    style = common("plan_style") or None
    issues.append({"type": kind, "severity": severity, "detail": detail, "suggestion": suggestion,
                   "actionable": actionable, "plan_style": style, "day": common("day"),
                   "date": common("date") or None, "block_ids": list(dict.fromkeys(b["id"] for b in blocks if b.get("id"))),
                   "evidence": list(evidence)})


def _budget_checks(plan, basic, rows, issues):
    key = "total_budget" if "total_budget" in basic else "budget"
    limit = _money(basic.get(key))
    if key in basic and limit is None:
        _add(issues, "预算", "medium", "用户预算无法解析，暂不能核实硬预算。", "请提供明确的总预算金额。",
             evidence=[_evidence(f"basic.{key}", basic[key])])
    groups = defaultdict(list)
    for row in rows:
        groups[row[0].get("plan_style") or "推荐方案"].append(row)
    reports = plan.get("cost_by_style") or {}
    missing_reports = plan.get("unpriced_items") or {}
    statuses = plan.get("budget_by_style") or {}
    if any(not isinstance(v, dict) for v in (reports, missing_reports, statuses)):
        raise ValueError("按方案费用元数据必须是对象")
    summary = {}
    for style, batch in groups.items():
        known = Decimal(0)
        unknown = []
        priced = []
        for b, path in batch:
            if b.get("type") not in COST_TYPES or (b.get("name") in UNNAMED_ACTIVITIES and "price" not in b):
                continue
            value = _money(b.get("price"))
            if (b.get("price_known") is False or b.get("price_source") == "unknown"
                    or ("unit_price" in b and b["unit_price"] is None)):
                value = None
            if value is None:
                unknown.append((b, path))
            else:
                known += value  # price already includes travelers; never multiply again.
                priced.append(_evidence(path + ".price", b["price"]))
        reported = _money(reports.get(style))
        reported_missing = missing_reports.get(style) or statuses.get(style) == "unknown"
        if reported is not None:
            if not unknown and reported != known:
                _add(issues, "预算", "medium", "方案费用汇总与已知活动价格之和不一致，本次按活动价格重算。",
                     "刷新方案费用汇总，避免沿用修改前的总价。", batch,
                     [*priced, _evidence(f"plan.cost_by_style[{json.dumps(style, ensure_ascii=False)}]", reports[style])])
            elif unknown:
                known = max(known, reported)
            priced.append(_evidence(f"plan.cost_by_style[{json.dumps(style, ensure_ascii=False)}]", reports[style]))
        state = "over" if limit is not None and known > limit else "unknown" if unknown or reported_missing else "ok" if limit is not None else "not_set"
        if not math.isfinite(float(known)):
            raise ValueError("金额超出可表示范围")
        summary[style] = {"known_cost": float(known), "budget_status": state, "unknown_count": len(unknown)}
        if state == "over":
            _add(issues, "预算", "high", f"「{style}」已知费用{known}元超过用户总预算{limit}元。",
                 "减少或替换付费项目，按该方案重新核算费用。", batch,
                 [*priced, _evidence(f"basic.{key}", basic[key])], actionable=True)
        if unknown or reported_missing:
            ev = [_evidence(path, b) for b, path in unknown]
            if style in missing_reports:
                ev.append(_evidence(f"plan.unpriced_items[{json.dumps(style, ensure_ascii=False)}]", missing_reports[style]))
            if style in statuses:
                ev.append(_evidence(f"plan.budget_by_style[{json.dumps(style, ensure_ascii=False)}]", statuses[style]))
            _add(issues, "预算", "medium", f"「{style}」有报价未知项目，已知小计不代表完整总费用。",
                 "预订前核实缺失报价；未知价格不能当作免费。", unknown or batch, ev)
    return summary


def _calendar(block, plan):
    result = _date(block.get("date"))
    if result:
        return result
    day = block.get("day")
    start = _date(plan.get("start_date"))
    if type(day) is int and day > 0:
        return (start or date(2000, 1, 1)) + timedelta(days=day - 1)
    return None


def _datetime(value, calendar):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed.astimezone(LOCAL_TZ)
        if calendar and re.fullmatch(r"\d{1,2}:\d{2}", value):
            hour, minute = map(int, value.split(":"))
            return datetime.combine(calendar, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=LOCAL_TZ)
    except ValueError:
        pass
    return None


def _span(block, plan):
    calendar = _calendar(block, plan)
    if block.get("type") == "交通":
        dep = block.get("dep_time") or block.get("departure_time")
        arr = block.get("arr_time") or block.get("arrival_time")
        if dep or arr:
            start, end = _datetime(dep, calendar), _datetime(arr, calendar)
            if start and end and end < start and not re.match(r"^\d{4}-", str(arr)):
                end += timedelta(days=1)
            return (start, end) if start and end and end > start else None
    match = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*[-—–~～至]\s*(\d{1,2}:\d{2})\s*", str(block.get("time") or ""))
    if not match:
        return None
    start, end = (_datetime(t, calendar) for t in match.groups())
    if start and end and end < start and block.get("type") == "交通":
        end += timedelta(days=1)
    return (start, end) if start and end and end > start else None


def _transport_buffer(block):
    if block.get("type") != "交通":
        return 0
    if block.get("flight_no") or block.get("kind") == "flight":
        return 120 * 60
    if block.get("train_no") or block.get("kind") == "train":
        return 60 * 60
    name = str(block.get("name") or "")
    if block.get("kind") in ("walk", "transit", "drive", "taxi", "bus", "metro") or re.search(r"打车|出租车|驾车|步行|地铁|公交|接驳", name):
        return 0
    if re.search(r"航班|飞机", name):
        return 120 * 60
    if re.search(r"高铁|火车|列车|动车", name):
        return 60 * 60
    return 0


def _time_checks(plan, rows, issues):
    timed = []
    for row in rows:
        b, path = row
        if b.get("type") in ("天气", "酒店"):
            continue
        span = _span(b, plan)
        if span is None:
            # A fully stated but reversed interval is a conflict, not missing data.
            calendar = _calendar(b, plan)
            dep, arr = b.get("dep_time") or b.get("departure_time"), b.get("arr_time") or b.get("arrival_time")
            exact = re.fullmatch(r"\s*(\d{1,2}:\d{2})\s*[-—–~～至]\s*(\d{1,2}:\d{2})\s*", str(b.get("time") or ""))
            endpoints = (_datetime(dep, calendar), _datetime(arr, calendar)) if dep and arr else tuple(_datetime(v, calendar) for v in exact.groups()) if exact else (None, None)
            if all(endpoints) and endpoints[1] <= endpoints[0] and (b.get("type") != "交通" or (dep and arr and re.match(r"^\d{4}-", str(arr)))):
                _add(issues, "时间", "high", "已明确的活动起止时间无效，结束时间不晚于开始时间。",
                     "修正起止时间；跨日活动请提供完整日期。", [row], [_evidence(path, b)], actionable=True)
                continue
            _add(issues, "时间", "medium", f"「{b.get('name') or '活动'}」缺少有效日期或完整时间区间，时间待核实。",
                 "补充活动起止时间；跨日班次请提供完整出发与到达日期。", [row], [_evidence(path, b)])
            continue
        timed.append((row, span))
    for i, (a, sa) in enumerate(timed):
        for b, sb in timed[i + 1:]:
            if a[0].get("plan_style") != b[0].get("plan_style"):
                continue
            if max(sa[0], sb[0]) < min(sa[1], sb[1]):
                _add(issues, "时间", "high", f"「{a[0].get('name')}」与「{b[0].get('name')}」时间重叠。",
                     "调整起止时间或减少活动，保留实际交通所需时间。", [a, b],
                     [_evidence(a[1], a[0]), _evidence(b[1], b[0])], actionable=True)
    ids = {row[0].get("id"): (row, span) for row, span in timed if row[0].get("id")}
    checked = set()
    legs = plan.get("legs") or []
    if not isinstance(legs, list):
        raise ValueError("legs必须是列表")
    observed = set()
    for li, leg in enumerate(legs):
        path = f"plan.legs[{li}]"
        if not isinstance(leg, dict):
            raise ValueError("路线结构无效")
        pair = (leg.get("from"), leg.get("to"))
        observed.add(pair)
        if pair in checked:
            continue
        if pair[0] not in ids or pair[1] not in ids:
            continue  # Overnight hotel legs have no scheduled interval to compare.
        a, sa = ids[pair[0]]
        b, sb = ids[pair[1]]
        if a[0].get("plan_style") != b[0].get("plan_style"):
            _add(issues, "交通", "medium", "路线端点不属于同一方案，无法核实耗时。", "更新该方案的路线。",
                 evidence=[_evidence(path, leg)])
            continue
        if leg.get("plan_style") is not None and leg["plan_style"] != b[0].get("plan_style"):
            _add(issues, "交通", "medium", "路线所属方案与活动不一致。", "重新计算当前方案路线。",
                 [a, b], [_evidence(path, leg)])
            continue
        if leg.get("day") is not None and leg["day"] != b[0].get("day"):
            _add(issues, "交通", "medium", "路线日期与终点活动不一致，不能用于核实耗时。", "更新对应日期的路线。",
                 [a, b], [_evidence(path, leg)])
            continue
        duration = _money(leg.get("duration_s"))
        if duration is None or duration <= 0:
            _add(issues, "交通", "medium", "路线缺少有效交通耗时，暂不能核实能否赶到。", "获取实际交通耗时。",
                 [a, b], [_evidence(path, leg)])
            continue
        checked.add(pair)
        gap = (sb[0] - sa[1]).total_seconds()
        buffer = _transport_buffer(b[0]) + _transport_buffer(a[0])
        status = route_gap_status(leg, gap, buffer) if gap >= 0 else "overlap"
        evidence = [_evidence(path + ".duration_s", leg["duration_s"]), _evidence(a[1], a[0]), _evidence(b[1], b[0])]
        if "mode" in leg:
            evidence.append(_evidence(path + ".mode", leg["mode"]))
        if "mode_locked" in leg:
            evidence.append(_evidence(path + ".mode_locked", leg["mode_locked"]))
        for index, alternative in enumerate(leg.get("alternatives") or []):
            if isinstance(alternative, dict):
                for field in ("mode", "duration_s"):
                    if field in alternative:
                        evidence.append(_evidence(f"{path}.alternatives[{index}].{field}", alternative[field]))
        mode = MODE_LABELS.get(leg.get("mode"), "当前路线")
        if status == "mode_unconfirmed":
            _add(issues, "交通", "medium", f"「{a[0].get('name')}」到「{b[0].get('name')}」间隔{gap / 60:g}分钟，"
                 f"当前{mode}路线约{math.ceil(float(duration) / 60)}分钟；其他交通方式尚未核实，不能据此认定无法到达。",
                 "可核实驾车或其他可用路线；若坚持当前方式，再调整活动时间。", [a, b], evidence)
        elif status == "insufficient":
            detail = f"「{a[0].get('name')}」到「{b[0].get('name')}」间隔{gap / 60:g}分钟，"
            detail += f"{mode}路线约{math.ceil(float(duration) / 60)}分钟"
            detail += f"，另需进出站预留{buffer / 60:g}分钟。" if buffer else "，当前路线耗时超出可用间隔。"
            suggestion = "提前结束前一活动或推迟后一活动，保留实际转场所需时间。"
            if buffer:
                suggestion += "航班120分钟、列车60分钟的预留仅适用于已选班次。"
            _add(issues, "交通", "high", detail, suggestion, [a, b], evidence, actionable=True)
    # The current planner reserves 120/60 minutes even without a mapped station route.
    groups = defaultdict(list)
    for row, span in timed:
        groups[row[0].get("plan_style")].append((row, span))
    for batch in groups.values():
        batch.sort(key=lambda x: x[1][0])
        for (a, sa), (b, sb) in zip(batch, batch[1:]):
            gap = (sb[0] - sa[1]).total_seconds()
            buffer = _transport_buffer(a[0]) + _transport_buffer(b[0])
            pair = (a[0].get("id"), b[0].get("id"))
            if (gap >= 0 and not buffer and pair not in observed and sa[1].date() == sb[0].date()
                    and a[0].get("type") in SOURCES and b[0].get("type") in SOURCES
                    and a[0].get("name") not in UNNAMED_ACTIVITIES and b[0].get("name") not in UNNAMED_ACTIVITIES
                    and _name(a[0].get("name")) != _name(b[0].get("name"))
                    and not (a[0].get("poi_id") and a[0].get("poi_id") == b[0].get("poi_id"))):
                _add(issues, "交通", "medium", "相邻地点缺少实际路线耗时，能否及时赶到待核实。", "补充这两个地点间的路线。",
                     [a, b], [_evidence(a[1], a[0]), _evidence(b[1], b[0])])
            if gap >= 0 and buffer and gap < buffer and pair not in checked:
                _add(issues, "交通", "high", "活动与已选班次之间不足以容纳项目规定的进出站预留。",
                     "按当前项目航班120分钟、列车60分钟规则调整活动时间。", [a, b],
                     [_evidence(a[1], a[0]), _evidence(b[1], b[0])], actionable=True)


def _names(source):
    values = [source.get("name") or source.get("title")]
    aliases = source.get("aliases")
    if isinstance(aliases, list):
        values.extend(v for v in aliases if isinstance(v, str))
    return {_name(v) for v in values if v}


def _identity(source):
    value = source.get("poi_id") or source.get("id")
    return str(value) if value is not None else ""


def _url(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def _links(source):
    links = {_url(source.get(k)) for k in ("link", "url", "map_url", "poi_detail_url", "booking_url")}
    if source.get("poi_id"):
        links.add(_url("https://www.amap.com/place/" + str(source["poi_id"])))
    return links - {None}


def _source_checks(search, rows, issues):
    named = matched = unavailable = ambiguous = 0
    for row in rows:
        b, path = row
        keys = SOURCES.get(b.get("type"))
        if not keys or b.get("name") in UNNAMED_ACTIVITIES:
            continue
        # The website stores the chosen option as a zero-based integer index;
        # legacy callers may supply a name. Zero is a valid selection.
        selected = b.get("selected_option")
        options = b.get("options") or []
        selected_index = type(selected) is int and isinstance(options, list) and 0 <= selected < len(options)
        selected_name = isinstance(selected, str) and bool(selected.strip())
        is_selected = b.get("user_selected") is True or selected_index or selected_name
        # Option groups are not a selected venue and must not count every alternative.
        if not is_selected and (b.get("name") in MEAL_LABELS or (b.get("options") and re.search(r"可选|餐饮推荐", str(b.get("name") or "") + str(b.get("note") or "")))):
            _add(issues, "真实性", "medium", "餐饮选项尚未确定实际地点，暂不计入已选地点来源覆盖率。",
                 "选择具体餐厅后再次检查。", [row], [_evidence(path, b)])
            continue
        name = selected if selected_name else b.get("name")
        if selected_index and (not name or name in MEAL_LABELS or "可选" in str(name)):
            option = options[selected]
            name = option.get("name") if isinstance(option, dict) else None
        if not isinstance(name, str) or not name.strip():
            _add(issues, "真实性", "medium", "活动缺少具体地点名称，来源待核实。", "补充地点名称和来源。", [row], [_evidence(path, b)])
            continue
        named += 1
        candidates = []
        for key in keys:
            value = search.get(key)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"search.{key}必须是来源列表")
            for si, source in enumerate(value or []):
                if not isinstance(source, dict):
                    raise ValueError("来源条目必须是对象")
                candidates.append((source, f"search.{key}[{si}]"))
        if not candidates:
            unavailable += 1
            _add(issues, "真实性", "medium", f"「{name}」缺少该类别搜索数据，不能确认来源。", "补充完整搜索快照再检查。",
                 [row], [_evidence(path, b), *[_evidence(f"search.{k}", search[k]) for k in keys if k in search]])
            continue
        identity = str(b.get("poi_id") or "")
        hits = [(s, sp) for s, sp in candidates if identity and _identity(s) == identity]
        names = [(s, sp) for s, sp in candidates if _name(name) in _names(s)]
        if hits and not any(_name(name) in _names(s) for s, _ in hits):
            _add(issues, "真实性", "high", f"「{name}」的地点ID与搜索来源名称不一致。", "使用来源中真实的地点名称与标识。",
                 [row], [_evidence(path, b), *[_evidence(sp, s) for s, sp in hits]], actionable=True)
            continue
        if not hits:
            hits = names
            if identity and hits and all(_identity(s) and _identity(s) != identity for s, _ in hits):
                _add(issues, "真实性", "high", f"「{name}」同名来源的地点ID不匹配，可能是不同分店或馆区。",
                     "按实际分店或馆区重新匹配地点ID。", [row], [_evidence(path, b), *[_evidence(sp, s) for s, sp in hits]], actionable=True)
                continue
        if not hits:
            partial = search.get("grounding_scope") == "selected"
            _add(issues, "真实性", "medium" if partial else "high", f"「{name}」未在提供的同类搜索候选中找到。",
                 "使用真实候选替换，或补充该地点的搜索来源；这不证明地点本身不存在。", [row],
                 [_evidence(path, b), *[_evidence(f"search.{k}", search[k]) for k in keys if k in search]], actionable=not partial)
            continue
        if not identity and len({_identity(s) for s, _ in hits if _identity(s)}) > 1:
            ambiguous += 1
            _add(issues, "真实性", "medium", f"「{name}」存在多个同名地点，不能确定具体分店。", "补充poi_id确认实际地点。",
                 [row], [_evidence(path, b), *[_evidence(sp, s) for s, sp in hits]])
            continue
        matched += 1
        block_links = [b[k] for k in ("link", "url", "map_url", "poi_detail_url") if b.get(k)]
        allowed = set().union(*(_links(s) for s, _ in hits))
        if not block_links or any(_url(link) is None or _url(link) not in allowed for link in block_links):
            _add(issues, "真实性", "medium", f"「{name}」的信息链接缺失或无法与来源对应。",
                 "使用搜索来源中的信息或预订链接。", [row], [_evidence(path, b), *[_evidence(sp, s) for s, sp in hits]])
    return {"named_count": named, "matched_count": matched, "unavailable_count": unavailable,
            "ambiguous_count": ambiguous, "coverage": round(matched / named, 4) if named else None}


def _input_value(context, path):
    """Resolve the paths generated by this module, including quoted style keys."""
    value = context
    tokens = re.findall(r'(?:^|\.)([A-Za-z_][A-Za-z_0-9]*)|\[(\d+|"(?:[^"\\]|\\.)*")\]', path)
    if not tokens:
        raise ValueError("invalid evidence path")
    for key, index in tokens:
        value = value[key] if key else value[json.loads(index)]
    return deepcopy(value)


def validate_rules(plan, search=None, basic=None):
    """Inspect complete snapshots; never call models, tools, network or mutate inputs."""
    try:
        if not isinstance(plan, dict) or (search is not None and not isinstance(search, dict)) or (basic is not None and not isinstance(basic, dict)):
            raise ValueError("plan/search/basic必须是对象")
        search, basic = search or {}, basic or {}
        json.dumps({"plan": plan, "search": search, "basic": basic}, allow_nan=False)
        rows = _rows(plan)
        issues = []
        if not rows:
            _add(issues, "完整性", "high", "方案没有活动，不能作为完整行程通过审核。", "先生成有效行程再审核。",
                 evidence=[_evidence("plan", plan)])
        budget = _budget_checks(plan, basic, rows, issues)
        _time_checks(plan, rows, issues)
        grounding = _source_checks(search, rows, issues)
        # Read values back from input paths rather than emitting normalized copies.
        # This also keeps inherited style/day fields out of nested source evidence.
        for issue in issues:
            for evidence in issue["evidence"]:
                evidence["value"] = _input_value({"plan": plan, "search": search, "basic": basic}, evidence["path"])
        passed = not any(i["severity"] == "high" for i in issues)
        feedback = "请修复规则发现的严重问题。" if not passed else "规则未发现严重冲突；未知信息和风险提示仍需核实。" if issues else "现有数据范围内规则检查通过。"
        return {**normalize_audit({"passed": passed, "issues": issues, "feedback": feedback}, plan, source="rule"),
                "rule_summary": {"budget_by_style": budget, "grounding": grounding}}
    except (ValueError, TypeError, AttributeError, OverflowError, KeyError, IndexError):
        return failed_audit("规则输入无效", "请检查方案、日期、活动ID、路线和来源数据结构。")


def main():
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict) or "plan" not in data:
            raise ValueError("missing plan")
        result = validate_rules(data["plan"], search=data.get("search"), basic=data.get("basic"))
    except (ValueError, TypeError):
        result = failed_audit("规则输入无效", "请通过标准输入提供包含plan的JSON对象。")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
