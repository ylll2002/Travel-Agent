"""Carry actual supplemental search results across subprocess boundaries."""
from copy import deepcopy


def merge_plan_sources(search, plan):
    result = deepcopy(search or {})
    if isinstance(plan.get("food"), list):
        result["food"] = deepcopy(plan["food"])
        result["food_by_anchor"] = deepcopy(plan.get("food_by_anchor") or [])
    updates = plan.get("source_updates") or {}
    if isinstance(updates, dict) and isinstance(updates.get("hotels"), list):
        hotels = result.setdefault("hotels", [])
        if isinstance(hotels, list):
            for candidate in updates["hotels"]:
                if isinstance(candidate, dict) and candidate not in hotels:
                    hotels.append(deepcopy(candidate))
    return result
