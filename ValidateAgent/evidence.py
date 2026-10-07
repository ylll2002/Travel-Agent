"""Resolve model evidence without eval, and separate facts from unsupported claims."""
import ast
import math
import re
import json
from copy import deepcopy
from ValidateAgent.rules import _money, _span, _transport_buffer
from shared.route_timing import route_gap_status

_TOKEN = re.compile(r"(?:([A-Za-z_]\w*)|\.([\w]+)|\[(\d+|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')\])")


def _path_keys(path):
    if not isinstance(path, str) or not 0 < len(path) <= 1024:
        raise ValueError("invalid evidence path")
    keys, pos = [], 0
    while pos < len(path):
        token = _TOKEN.match(path, pos)
        if token is None or len(keys) >= 40:
            raise ValueError("invalid evidence path")
        root, field, bracket = token.groups()
        if (pos == 0) != (root is not None):
            raise ValueError("invalid evidence path")
        key = root or field
        if bracket is not None:
            try:
                key = (json.loads(bracket) if bracket.startswith('"') else
                       ast.literal_eval(bracket) if bracket.startswith("'") else int(bracket))
            except (ValueError, SyntaxError) as exc:
                raise ValueError("invalid evidence key") from exc
        keys.append(key)
        pos = token.end()
    return keys


def read_path(context, path):
    value = context
    for key in _path_keys(path):
        if isinstance(key, int):
            if not isinstance(value, list) or key >= len(value):
                raise ValueError("evidence index absent")
        elif not isinstance(value, dict) or key not in value:
            raise ValueError("evidence field absent")
        value = value[key]
    return value



def _field_path(keys):
    path = keys[0]
    for key in keys[1:]:
        path += (f"[{key}]" if isinstance(key, int) else
                 "." + key if re.fullmatch(r"[A-Za-z_]\w*", key) else
                 "[" + json.dumps(key, ensure_ascii=False) + "]")
    return path


def _model_path(path):
    if not isinstance(path, str):
        raise ValueError("invalid evidence path")
    path = path.strip()
    if path.startswith("`") and path.endswith("`"):
        path = path[1:-1].strip()
    if path.startswith("$."):
        path = path[2:]
    keys = _path_keys(path)
    if keys[0] not in {"plan", "search", "basic", "user_profile", "preferences", "recent_trips"}:
        raise ValueError("invalid evidence root")
    return _field_path(keys)


def _leaves(value, path, limit=64, depth=0):
    """Enumerate exact input leaves; empty collections are real missing-data facts."""
    if depth > 32:
        raise ValueError("evidence subtree too deep")
    if not isinstance(value, (dict, list)) or not value:
        return [{"path": path, "value": deepcopy(value)}]
    result = []
    entries = value.items() if isinstance(value, dict) else enumerate(value)
    for key, child in entries:
        suffix = f"[{key}]" if isinstance(key, int) else (
            "." + key if re.fullmatch(r"[A-Za-z_]\w*", key) else
            "[" + json.dumps(key, ensure_ascii=False) + "]")
        result.extend(_leaves(child, path + suffix, limit, depth + 1))
        if len(result) > limit:
            raise ValueError("evidence subtree too broad; cite specific fields")
    return result


def _nearby_paths(context, path):
    """Small, data-derived retry hints; never guess field aliases or array offsets."""
    try:
        keys = _path_keys(_model_path(path))
    except ValueError:
        return []
    value, matched = context, []
    for key in keys:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            break
        matched.append(key)
    if not matched:
        return []
    # Walk only a few branches, without serializing unrelated source records.
    hints = []
    def visit(node, prefix, depth=0):
        if len(hints) >= 8 or depth > 12:
            return
        if not isinstance(node, (dict, list)) or not node:
            hints.append(_field_path(prefix))
            return
        entries = node.items() if isinstance(node, dict) else enumerate(node)
        for key, child in entries:
            visit(child, [*prefix, key], depth + 1)
            if len(hints) >= 8:
                break
    visit(value, matched)
    return hints


def evidence_path_examples(context):
    paths = []
    for path in ("search.weather", "basic", "plan.blocks[0]"):
        try:
            read_path(context, path)
        except ValueError:
            continue
        paths.extend(_nearby_paths(context, path)[:4])
    return paths


class EvidencePathError(ValueError):
    def __init__(self, problems):
        super().__init__("invalid model evidence paths")
        self.retry_hint = "以下引用无效，请按实际输入修正；候选字段只表示路径存在，不代表支持该问题：" + json.dumps(
            problems[:6], ensure_ascii=False)


def materialize_model_evidence(raw, context):
    """Resolve exact facts, accepting harmless syntax and bounded object references.

    Legacy path/value records keep their supplied values for subsequent forgery
    checks. Missing fields are never guessed or silently removed.
    """
    result = deepcopy(raw)
    if not isinstance(result, dict) or not isinstance(result.get("issues"), list):
        return result
    problems = []
    for issue_index, issue in enumerate(result["issues"]):
        if not isinstance(issue, dict) or not isinstance(issue.get("evidence"), list):
            continue
        evidence = []
        for item in issue["evidence"]:
            if isinstance(item, str) or (isinstance(item, dict) and "path" in item and "value" not in item):
                requested = item if isinstance(item, str) else item["path"]
                try:
                    path = _model_path(requested)
                    value = read_path(context, path)
                    if isinstance(value, (dict, list)) and value and (
                            len(_path_keys(path)) == 1 or path in {"plan.blocks", "plan.plans", "plan.legs"}):
                        raise ValueError("evidence reference too broad; cite specific fields")
                    evidence.extend(_leaves(value, path))
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    problems.append({"issue": issue_index, "path": str(requested)[:1024],
                                     "reason": str(exc), "available_paths": _nearby_paths(context, requested)})
            else:
                # Normalize harmless notation without replacing legacy supplied values.
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    try:
                        item["path"] = _model_path(item["path"])
                    except ValueError:
                        pass  # The verifier will remove unsupported legacy facts.
                evidence.append(item)
        issue["evidence"] = evidence
    if problems:
        raise EvidencePathError(problems)
    return result


def same_value(left, right):
    # Python's True == 1 must never make a forged evidence value valid.
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isfinite(left) and math.isfinite(right) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    return left == right


_QUOTE_FIELDS = {"price", "unit_price", "price_per_person", "ticket_price", "ticketPrice", "nightly_price"}


def _quote_fact(keys, value, context):
    """Only quoted leaves count as amounts; a missing quote is not spending evidence."""
    if not keys or keys[0] not in ("plan", "search"):
        return None, None
    field = keys[-1]
    if (field == "price_known" and value is False) or (field == "price_source" and value == "unknown"):
        return "missing", None
    if field not in _QUOTE_FIELDS:
        return None, None
    parent = context
    for key in keys[:-1]:
        parent = parent[key]
    unavailable = isinstance(parent, dict) and (parent.get("price_known") is False or
        parent.get("price_source") == "unknown" or ("unit_price" in parent and parent["unit_price"] is None))
    amount = _money(value)
    return ("missing", None) if unavailable or amount is None else ("known", amount)


def _missing_quote_only(facts, context):
    """Price-only evidence cannot support a high completeness/budget claim.

    Operational evidence (hours, weather, time, allergies etc.) keeps its original
    checks; text mentioning a quote never overrides an actual execution conflict.
    """
    missing = False
    for keys, value in facts:
        state, _ = _quote_fact(keys, value, context)
        if state == "missing":
            missing = True
            continue
        if keys[0] == "basic" and keys[1:2] in (["hard_limits"], ["total_budget"], ["budget"]):
            continue
        if keys[0] == "plan" and keys[1:2] == ["unpriced_items"]:
            missing = True
            continue
        if keys[0] == "plan" and keys[1:2] in (["budget_status"], ["budget_by_style"]) and value == "unknown":
            missing = True
            continue
        if keys[0] == "plan" and keys[1:2] == ["cost_by_style"]:
            continue  # A subtotal is context, not evidence of an unquoted charge.
        if keys[-1] in ("name", "id", "plan_style", "day", "date"):
            continue
        return False
    return missing


def _unproven_local_route_conflict(issue, facts, context):
    """Clock/route facts cannot justify a stricter local-mode assumption than rules."""
    if issue["type"] not in ("交通", "时间") or not facts:
        return False
    fields = {"id", "name", "type", "time", "date", "day", "plan_style", "lng", "lat", "poi_id",
              "from", "to", "mode", "mode_locked", "distance_m", "duration_s"}
    if any(keys[:2] not in (["plan", "blocks"], ["plan", "legs"]) or
           keys[-1] not in fields or isinstance(value, (dict, list)) for keys, value in facts):
        return False
    plan = context.get("plan") or {}
    blocks = {block.get("id"): block for block in plan.get("blocks") or [] if isinstance(block, dict)}
    cited = {keys[2] for keys, _ in facts if keys[:2] == ["plan", "legs"] and type(keys[2]) is int}
    pairs = [leg for index, leg in enumerate(plan.get("legs") or []) if isinstance(leg, dict) and
             (index in cited or (issue["block_ids"] and {leg.get("from"), leg.get("to")} <= set(issue["block_ids"])))]
    if not pairs:
        return False
    for leg in pairs:
        a, b = blocks.get(leg.get("from")), blocks.get(leg.get("to"))
        if not a or not b or a.get("type") == "交通" or b.get("type") == "交通":
            return False
        sa, sb = _span(a, plan), _span(b, plan)
        if not sa or not sb or a.get("plan_style") != b.get("plan_style"):
            return False
        gap = (sb[0] - sa[1]).total_seconds()
        if gap < 0 or route_gap_status(leg, gap, _transport_buffer(a) + _transport_buffer(b)) not in ("sufficient", "mode_unconfirmed", "unknown"):
            return False
    return True


def verify_model_issues(audit, original, sent):
    result = deepcopy(audit)
    if result.get("error"):
        return result
    basic = original.get("basic") or {}
    # Global monetary totals are owned by the rules. Only explicit numeric
    # specialty caps allow an additional model budget blocker.
    caps = basic.get("hard_limits") or {}
    def positive_number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            return False
    for issue in result["issues"]:
        evidence, invalid = [], False
        for item in issue["evidence"]:
            try:
                actual = read_path(original, item["path"])
                visible = read_path(sent, item["path"])
                valid = same_value(actual, item["value"]) and same_value(visible, item["value"])
            except (ValueError, KeyError, IndexError, TypeError, OverflowError):
                valid = False
            if valid:
                evidence.append({"path": item["path"], "value": deepcopy(actual)})
            else:
                invalid = True
        issue["evidence"] = evidence
        unsupported = invalid or not evidence
        facts = [(_path_keys(e["path"]), e["value"]) for e in evidence]
        cited_caps = [value for keys, value in facts if isinstance(caps, dict)
            and keys[:2] == ["basic", "hard_limits"] and len(keys) == 3 and positive_number(value)]
        cited_amounts = [amount for keys, value in facts
            for state, amount in [_quote_fact(keys, value, original)] if state == "known"]
        soft_budget = issue["type"] == "预算" and not any(
            amount > cap for cap in cited_caps for amount in cited_amounts)
        missing_quote_only = _missing_quote_only(facts, original)
        unproven_local_route = _unproven_local_route_conflict(issue, facts, original)
        preference_without_requirement = issue["type"] == "偏好" and not any(
            (keys[0] == "basic" and len(keys) > 1) or
            (keys[0] == "user_profile" and len(keys) > 1 and isinstance(keys[1], str)
             and any(k in keys[1].casefold() for k in
                     ("allerg", "diet", "accessibility", "忌口", "过敏", "特殊需求"))) for keys, _ in facts)
        weather_without_forecast = issue["type"] == "天气" and not any(
            keys[:2] == ["search", "weather"] and value not in (None, "", [], {}) for keys, value in facts)
        if issue["severity"] == "high" and (unsupported or missing_quote_only or soft_budget or unproven_local_route or
                                           preference_without_requirement or weather_without_forecast):
            issue.update(severity="medium", actionable=False)
            issue["detail"] += ("（暂无报价仅需预订前核实，不作为阻断行程的理由。）" if missing_quote_only else
                                "（模型预算判断缺少已知费用超出专项硬上限的证据；总预算以规则核算为准。）" if soft_budget else
                                "（历史偏好未由本次明确要求支持，作为优化建议。）" if preference_without_requirement else
                                "（缺少实际天气证据，预报缺失不能当作恶劣天气。）" if weather_without_forecast else
                                "（市内路线已有可行方式或其他方式尚未核实，不能仅按默认方式判定无法到达。）" if unproven_local_route else
                                "（严重程度缺少可核对的输入证据，暂作为待核实提示。）")
        elif invalid:
            issue["actionable"] = False
            issue["detail"] += "（部分模型证据无法对应输入，已移除，需核实。）"
    # The hybrid verdict is derived from verified severity, not free-text feedback.
    result["passed"] = not any(i["severity"] == "high" for i in result["issues"])
    return result
