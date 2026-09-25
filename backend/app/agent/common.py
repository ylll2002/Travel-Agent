"""Agent 共用的 LLM、响应解析和行程卡片标准化工具。"""

import json
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config import get_settings


def _parse_json_response(content: str):
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _message_content(response) -> str:
    return response.content if isinstance(response.content, str) else json.dumps(response.content, ensure_ascii=False)


def _normalise_draft_cards(draft: list[dict[str, Any]]) -> tuple[list[dict], dict[str, list[str]]]:
    cards: list[dict] = []
    dependencies: dict[str, list[str]] = {}
    previous_by_day: dict[Any, str] = {}
    for index, raw_card in enumerate(draft, start=1):
        card = dict(raw_card)
        card_id = str(card.get("card_id") or f"card-{index}")
        day = card.get("day", 1)
        dependency_ids = list(card.get("dependency_ids") or [])
        previous_card_id = previous_by_day.get(day)
        if previous_card_id and previous_card_id not in dependency_ids:
            dependency_ids.append(previous_card_id)
        card.update({"card_id": card_id, "dependency_ids": dependency_ids, "evidence_refs": list(card.get("evidence_refs") or []), "status": card.get("status", "estimate")})
        cards.append(card)
        dependencies[card_id] = dependency_ids
        previous_by_day[day] = card_id
    return cards, dependencies


def get_llm():
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        api_key=SecretStr(settings.openai_api_key) if settings.openai_api_key else None,
        base_url=settings.openai_api_base or None,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        max_completion_tokens=settings.llm_max_tokens,
    )
