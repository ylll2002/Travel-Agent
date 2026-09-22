from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Destination

SAMPLE_DESTINATIONS = [
    {
        "name": "东京",
        "country": "日本",
        "description": "现代都市与传统文化的交汇。",
        "tags": "城市,美食,购物",
    },
    {
        "name": "巴黎",
        "country": "法国",
        "description": "浪漫与艺术之都。",
        "tags": "城市,艺术,浪漫",
    },
    {
        "name": "巴厘岛",
        "country": "印度尼西亚",
        "description": "海岛度假胜地。",
        "tags": "海岛,度假,自然",
    },
    {
        "name": "大理",
        "country": "中国",
        "description": "苍山洱海，风花雪月。",
        "tags": "自然,古镇,慢生活",
    },
]


def seed_destinations(db: Session) -> None:
    """Insert sample destinations once, if the table is empty."""
    if db.scalar(select(Destination).limit(1)) is not None:
        return
    for item in SAMPLE_DESTINATIONS:
        db.add(Destination(**item))
    db.commit()

