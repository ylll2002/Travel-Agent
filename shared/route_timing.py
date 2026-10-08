"""Interpret verified route times without treating an automatic mode as a requirement."""
import math

MODE_LABELS = {"walk": "步行", "transit": "公交/地铁", "drive": "驾车/打车"}
LOCAL_TRANSFER_PADDING_S = 5 * 60


def route_seconds(route):
    value = route.get("duration_s") if isinstance(route, dict) else None
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def route_gap_status(leg, gap_s, buffer_s=0):
    """Return sufficient, insufficient or mode_unconfirmed from existing evidence.

    An unlocked walk/transit default exceeding a gap does not prove that driving
    is impossible. A selected mode, an actual driving route, or transport-station
    buffers can still establish a hard conflict. No durations are estimated here.
    """
    options = [leg]
    if leg.get("mode_locked") is not True:
        options += [option for option in leg.get("alternatives") or [] if isinstance(option, dict)]
    durations = [seconds for option in options if (seconds := route_seconds(option)) is not None]
    if not durations:
        return "unknown"
    if min(durations) + buffer_s <= gap_s:
        return "sufficient"
    driving_verified = any(option.get("mode") == "drive" and route_seconds(option) is not None for option in options)
    if not buffer_s and leg.get("mode_locked") is not True and leg.get("mode") in ("walk", "transit") and not driving_verified:
        return "mode_unconfirmed"
    return "insufficient"
