"""Recover scheduled travel metadata without guessing airports or train stations."""
import re


def travel_rows(search):
    rows = []
    for source in ("flights", "trains"):
        items = search.get(source) or []
        if isinstance(items, dict):
            items = [*(items.get("outbound") or []), *(items.get("inbound") or [])]
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, dict):
                continue
            rows.append({**row, "name": f"{row.get('airline') or row.get('transport') or ''}{row.get('flight_no') or row.get('train_no') or ''}", "_travel": True})
    return rows


def match_travel(block, search):
    name = re.sub(r"\s+", "", str(block.get("name") or "")).upper()
    codes = {str(block.get(key) or "").replace(" ", "").upper() for key in ("flight_no", "train_no")}
    codes.discard("")
    matches = []
    for row in travel_rows(search):
        code = str(row.get("flight_no") or row.get("train_no") or "").replace(" ", "").upper()
        exact = name == re.sub(r"\s+", "", row["name"]).upper()
        if exact or (code and (code in codes or re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", name))):
            matches.append(row)
    date = str(block.get("date") or "")
    if date:
        matches = [r for r in matches if not r.get("dep_time") or str(r["dep_time"]).startswith(date)]
    clock = re.search(r"\d{1,2}:\d{2}", str(block.get("time") or ""))
    if len(matches) > 1 and clock:
        timed = [r for r in matches if clock[0] in str(r.get("dep_time") or "")]
        if timed:
            matches = timed
    # Ambiguous flights (e.g. different airports) must never be silently chosen.
    variants = {(r.get("dep_station"), r.get("arr_station"), r.get("dep_time"), r.get("arr_time")) for r in matches}
    return matches[0] if matches and len(variants) == 1 else {}


def travel_metadata(block, search):
    row = match_travel(block, search)
    fields = ("dep_station", "arr_station", "dep_time", "arr_time", "flight_no", "train_no", "direction")
    result = {key: block.get(key) or row.get(key) for key in fields if block.get(key) or row.get(key)}
    # Legacy plans often keep both stations only in their display note.
    note = str(block.get("note") or "").strip()
    pair = re.fullmatch(r"([^→\n]+?)\s*(?:→|->)\s*([^→\n]+)", note)
    if pair and all(re.search(r"(?:机场|车站|站)$", part.strip()) for part in pair.groups()):
        result.setdefault("dep_station", pair[1].strip())
        result.setdefault("arr_station", pair[2].strip())
    direction = result.get("direction")
    if direction in ("回", "返", "回程", "返程", "inbound"):
        result["direction"] = "返"
    elif direction in ("去", "去程", "outbound"):
        result["direction"] = "去"
    elif "回程" in str(block.get("name")) or "返程" in str(block.get("name")):
        result["direction"] = "返"
    elif "去程" in str(block.get("name")):
        result["direction"] = "去"
    return result


def is_local_transport(block, search):
    if block.get("transport_scope") == "local":
        return True
    if travel_metadata(block, search) or re.search(r"(?i)[A-Z0-9]{1,3}\d{2,5}|航班|飞机|火车|高铁|去程|回程|返程", str(block.get("name") or "")):
        return False
    return bool(re.search(r"地铁|打车|出租车|公交|步行|自驾|换乘|接送|前往|返回|乘车|交通", str(block.get("name") or "")))


def attach_travel_metadata(blocks, search):
    for block in blocks:
        if block.get("type") != "交通":
            continue
        block.update(travel_metadata(block, search))
        if is_local_transport(block, search):
            block["transport_scope"] = "local"
    return blocks
