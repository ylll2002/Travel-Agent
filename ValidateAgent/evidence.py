"""Resolve model evidence without eval, and separate facts from unsupported claims."""
import math
import re
import json
from copy import deepcopy

_TOKEN = re.compile(r'(?:([A-Za-z_]\w*)|\.([\w]+)|\[(\d+|"(?:[^"\\]|\\.)*")\])')


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
            key = json.loads(bracket) if bracket.startswith('"') else int(bracket)
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
        soft_budget = issue["type"] == "预算" and not any(
            isinstance(caps, dict) and keys[:2] == ["basic", "hard_limits"] and len(keys) == 3
            and positive_number(value) for keys, value in facts)
        preference_without_requirement = issue["type"] == "偏好" and not any(
            (keys[0] == "basic" and len(keys) > 1) or
            (keys[0] == "user_profile" and len(keys) > 1 and isinstance(keys[1], str)
             and any(k in keys[1].casefold() for k in
                     ("allerg", "diet", "accessibility", "忌口", "过敏", "特殊需求"))) for keys, _ in facts)
        weather_without_forecast = issue["type"] == "天气" and not any(
            keys[:2] == ["search", "weather"] and value not in (None, "", [], {}) for keys, value in facts)
        if issue["severity"] == "high" and (unsupported or soft_budget or
                                           preference_without_requirement or weather_without_forecast):
            issue.update(severity="medium", actionable=False)
            issue["detail"] += ("（模型预算判断未由专项硬上限支持；总预算以规则核算为准。）" if soft_budget else
                                "（历史偏好未由本次明确要求支持，作为优化建议。）" if preference_without_requirement else
                                "（缺少实际天气证据，预报缺失不能当作恶劣天气。）" if weather_without_forecast else
                                "（严重程度缺少可核对的输入证据，暂作为待核实提示。）")
        elif invalid:
            issue["actionable"] = False
            issue["detail"] += "（部分模型证据无法对应输入，已移除，需核实。）"
    # The hybrid verdict is derived from verified severity, not free-text feedback.
    result["passed"] = not any(i["severity"] == "high" for i in result["issues"])
    return result
