"""Auto-tag suggestion using local LLM."""

from __future__ import annotations

import logging
import re

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a content tagger. Given an article title and text, "
    "suggest 1-3 broad, reusable category tags (lowercase). "
    "Prefer general topics (e.g. 'ai', 'python', 'security') over specific details. "
    "If a list of existing tags is provided, reuse them whenever appropriate "
    "instead of creating new ones. "
    "Return ONLY a comma-separated list of tags, nothing else."
)


async def suggest_tags(title: str, text: str, existing_tags: list[str] | None = None) -> list[str]:
    """Suggest tags for an article. Returns empty list if LLM is unavailable."""
    content = text[:2000]
    user_parts = []
    if existing_tags:
        user_parts.append(f"Existing tags: {', '.join(existing_tags)}")
    user_parts.append(f"Title: {title}\n\n{content}")
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    result = await chat_completion(messages, max_tokens=60, temperature=0.2)
    if not result:
        return []
    tags = [t.strip().lower().strip('"\'') for t in re.split(r"[,\n]", result)]
    return [t for t in tags if t and len(t) < 50][:3]
