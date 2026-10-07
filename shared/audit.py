"""Versioned audit contract shared by validation and orchestration (no SDK dependency)."""
from copy import deepcopy
from datetime import date

SCHEMA_VERSION = 1
ISSUE_TYPES = {"预算", "交通", "偏好", "天气", "时间", "完整性", "真实性"}


def _plan_context(plan):
    plan = plan if isinstance(plan, dict) else {}
    records = [b for b in plan.get("blocks") or [] if isinstance(b, dict)]
    styles = list(dict.fromkeys(b.get("plan_style") for b in records
                              if isinstance(b.get("plan_style"), str) and b["plan_style"]))
    for item in plan.get("plans") or []:
        if not isinstance(item, dict):
            continue
        style = item.get("style")
        if isinstance(style, str) and style:
            if style not in styles:
                styles.append(style)
            records.append({"plan_style": style})
        for day in item.get("itinerary") or []:
            if not isinstance(day, dict):
                continue
            records.append({"plan_style": style, "day": day.get("day"), "date": day.get("date")})
            for block in day.get("schedule") or []:
                if isinstance(block, dict):
                    records.append({**block, "plan_style": style, "day": day.get("day"), "date": day.get("date")})
    return records, styles


def _revision(plan):
    value = plan.get("revision") if isinstance(plan, dict) else None
    return value if type(value) is int and value >= 0 else None


def _issue(raw, records, source):
    if not isinstance(raw, dict):
        raise ValueError("issue must be an object")
    severity = raw.get("severity")
    kind = raw.get("type", "完整性")
    if severity not in ("high", "medium", "low") or kind not in ISSUE_TYPES:
        raise ValueError("invalid issue severity or type")
    for key in ("detail", "suggestion"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError("issue requires detail and suggestion")
    actionable = raw.get("actionable", False)
    if not isinstance(actionable, bool):
        raise ValueError("actionable must be a boolean")
    style, day, calendar = (raw.get(k) for k in ("plan_style", "day", "date"))
    if style is not None and (not isinstance(style, str) or not style.strip()):
        raise ValueError("invalid plan_style")
    if day is not None and (type(day) is not int or day < 1):
        raise ValueError("invalid day")
    if calendar is not None:
        if not isinstance(calendar, str) or date.fromisoformat(calendar).isoformat() != calendar:
            raise ValueError("invalid date")
    ids = raw.get("block_ids", [])
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("invalid block_ids")
    if any(v is not None for v in (style, day, calendar)) or ids:
        scoped = [b for b in records if all(v is None or b.get(k) == v for k, v in
                  (("plan_style", style), ("day", day), ("date", calendar)))]
        if not scoped or set(ids) - {b.get("id") for b in scoped}:
            raise ValueError("issue location does not belong to reviewed plan")
    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list) or any(not isinstance(e, dict) or
            not isinstance(e.get("path"), str) or not e["path"].strip() or "value" not in e for e in evidence):
        raise ValueError("evidence requires path and value")
    return {"severity": severity, "type": kind, "detail": raw["detail"].strip(),
            "suggestion": raw["suggestion"].strip(), "actionable": actionable,
            "source": source, "plan_style": style, "day": day, "date": calendar,
            "block_ids": list(ids), "evidence": [{"path": e["path"], "value": deepcopy(e["value"])} for e in evidence]}


def failed_audit(error, feedback, plan=None):
    _, styles = _plan_context(plan)
    return {"schema_version": SCHEMA_VERSION, "status": "error", "passed": False,
            "plan_revision": _revision(plan), "reviewed_styles": styles,
            "issues": [], "feedback": feedback, "error": error}


def normalize_audit(raw, plan=None, source="model"):
    """Validate structure and locations; never turn a failed verdict into a pass.

    Legacy issue locations/evidence may be absent. This does not prove grounding;
    evidence verification belongs to the deterministic rules in the next stage.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("passed"), bool):
        raise ValueError("audit requires boolean passed")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError("unsupported audit schema")
    if source not in ("model", "rule"):
        raise ValueError("invalid issue source")
    if raw.get("error"):
        return failed_audit(str(raw["error"]), str(raw.get("feedback") or "审核未能完成"), plan)
    issues = raw.get("issues")
    if not isinstance(issues, list):
        raise ValueError("audit requires issues list")
    feedback = raw.get("feedback", "")
    if not isinstance(feedback, str):
        raise ValueError("feedback must be text")
    records, styles = _plan_context(plan)
    normalized = [_issue(i, records, source) for i in issues]
    if raw["passed"] and any(i["severity"] == "high" for i in normalized):
        raise ValueError("passing verdict contradicts high severity issue")
    status = "blocked" if not raw["passed"] else "warning" if normalized else "passed"
    return {"schema_version": SCHEMA_VERSION, "status": status, "passed": raw["passed"],
            "plan_revision": _revision(plan), "reviewed_styles": styles,
            "issues": normalized, "feedback": feedback}


def high_actionable_issues(audit):
    if not isinstance(audit, dict) or audit.get("error") or not isinstance(audit.get("issues"), list):
        return []
    return [i for i in audit["issues"] if isinstance(i, dict) and i.get("severity") == "high"
            and i.get("actionable") is True and isinstance(i.get("detail"), str) and i["detail"].strip()
            and isinstance(i.get("suggestion"), str) and i["suggestion"].strip()]


def repair_feedback(audit):
    import json
    issues = high_actionable_issues(audit)
    if audit.get("passed") is not False or not issues:
        return None
    return ("审核发现以下有证据、影响执行且可修复的严重问题。请针对这些问题重新规划，"
            "保持用户的目的地、日期、硬预算及偏好，使用已有真实候选；"
            "不要把轻微优化建议当成新增硬约束，也不要捏造未知报价。严重问题："
            + json.dumps(issues, ensure_ascii=False))


def history_entry(iteration, audit):
    return {"iteration": iteration, **{k: deepcopy(audit[k]) for k in
            ("schema_version", "status", "passed", "plan_revision", "reviewed_styles", "issues", "feedback", "error") if k in audit}}
