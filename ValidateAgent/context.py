"""Trim duplicated/map-only data without losing source coverage or array indices."""
from copy import deepcopy


def plan_for_model(plan):
    if not isinstance(plan, dict):
        return plan
    trimmed = deepcopy(plan)
    for key in ("food", "food_by_anchor", "source_updates"):
        trimmed.pop(key, None)
    for leg in trimmed.get("legs") or []:
        if isinstance(leg, dict):
            leg.pop("polyline", None)
            for alternative in leg.get("alternatives") or []:
                if isinstance(alternative, dict):
                    alternative.pop("polyline", None)
    # Keep meal options: an unselected group must not look like a selected venue.
    return trimmed


def search_for_model(search, plan=None):
    # Keep every candidate, including last-ranked transport and source aliases.
    # Removing/reordering entries would also change evidence paths.
    keys = ("destination", "origin", "start_date", "end_date", "weather", "poi", "hotels",
            "food", "food_by_anchor", "events", "promotions", "flights", "trains", "grounding_scope")
    return {k: deepcopy(search[k]) for k in keys if k in (search or {})}
