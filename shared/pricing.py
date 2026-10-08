"""Parse RMB prices without treating missing or ambiguous values as free."""
import math
import re
import unicodedata

_PRICE = re.compile(
    r"(?:¥|RMB|CNY|人民币)?\s*"
    r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?:元)?\s*(?:起)?\s*(?:/(?:晚|人|份|次)|每(?:晚|人|份|次))?",
    re.IGNORECASE,
)


def parse_price(value) -> float | None:
    """Currency prefixes and valid thousand separators are supported.

    Ranges, masked prices, foreign currencies and malformed data stay unknown.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            amount = float(value)
        except OverflowError:
            return None
        return amount if math.isfinite(amount) and amount >= 0 else None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text in ("免费", "免票"):
        return 0.0
    match = _PRICE.fullmatch(text)
    if not match:
        return None
    amount = float(match["amount"].replace(",", ""))
    return amount if math.isfinite(amount) else None


def item_price(item: dict) -> float | None:
    if item.get("free") is True:
        return 0.0
    for key in ("price", "price_per_person", "ticketPrice"):
        parsed = parse_price(item.get(key))
        if parsed is not None:
            return parsed
    return None


def price_sort_key(value, descending=False):
    """Unknown prices sort after known prices in either direction."""
    parsed = parse_price(value)
    return (parsed is None, (-parsed if descending else parsed) if parsed is not None else 0)
