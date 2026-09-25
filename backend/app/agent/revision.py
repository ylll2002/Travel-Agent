"""行程局部修订的校验能力。"""

from typing import Any


def validate_revision_cards(original_cards: list[dict[str, Any]], revised_cards: object) -> tuple[bool, str]:
    """确保局部重规划没有漏卡、增卡或改变卡片结构身份。"""
    if not isinstance(revised_cards, list) or not all(isinstance(card, dict) for card in revised_cards):
        return False, "局部重规划返回的不是卡片对象数组"

    original_by_id = {str(card.get("card_id")): card for card in original_cards}
    returned_ids = [str(card.get("card_id")) for card in revised_cards]
    if any(card.get("card_id") is None for card in revised_cards):
        return False, "局部重规划返回了缺少 card_id 的卡片"
    if len(returned_ids) != len(set(returned_ids)):
        return False, "局部重规划返回了重复的 card_id"
    if set(returned_ids) != set(original_by_id):
        return False, "局部重规划必须完整返回原卡片，且不能新增未知 card_id"

    for revised in revised_cards:
        original = original_by_id[str(revised["card_id"])]
        if revised.get("day") != original.get("day"):
            return False, f"卡片 {revised['card_id']} 不允许修改 day"
        if revised.get("type") != original.get("type"):
            return False, f"卡片 {revised['card_id']} 不允许修改 type"
    return True, "局部重规划返回值通过结构校验"
