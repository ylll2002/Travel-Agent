from app.agent.graph import _validate_revision_cards


def test_revision_cards_accepts_complete_local_revision():
    original = [
        {"card_id": "card-1", "day": 1, "type": "activity", "title": "公园"},
        {"card_id": "card-2", "day": 1, "type": "dining", "title": "午餐"},
    ]
    revised = [
        {"card_id": "card-1", "day": 1, "type": "activity", "title": "博物馆"},
        {"card_id": "card-2", "day": 1, "type": "dining", "title": "午餐"},
    ]

    valid, message = _validate_revision_cards(original, revised)

    assert valid is True
    assert "通过" in message


def test_revision_cards_rejects_missing_or_unknown_card():
    original = [
        {"card_id": "card-1", "day": 1, "type": "activity"},
        {"card_id": "card-2", "day": 1, "type": "dining"},
    ]
    revised = [{"card_id": "card-1", "day": 1, "type": "activity"}]

    valid, message = _validate_revision_cards(original, revised)

    assert valid is False
    assert "完整返回" in message


def test_revision_cards_rejects_day_or_type_changes():
    original = [{"card_id": "card-1", "day": 1, "type": "activity"}]
    revised = [{"card_id": "card-1", "day": 2, "type": "activity"}]

    valid, message = _validate_revision_cards(original, revised)

    assert valid is False
    assert "不允许修改 day" in message