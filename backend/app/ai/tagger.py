"""Auto-tag suggestion using local LLM."""

from __future__ import annotations

import logging
import re

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a content tagger. Given an article title and text, "
    "suggest 1-5 short tags (single words or short phrases, lowercase). "
    "Return ONLY a comma-separated list of tags, nothing else. "
    "Example: python, web development, tutorial"
)


async def suggest_tags(title: str, text: str) -> list[str]:
    """Suggest tags for an article. Returns empty list if LLM is unavailable."""
    content = text[:2000]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Title: {title}\n\n{content}"},
    ]
    result = await chat_completion(messages, max_tokens=100, temperature=0.3)
    if not result:
        return []
    tags = [t.strip().lower().strip('"\'') for t in re.split(r"[,\n]", result)]
    return [t for t in tags if t and len(t) < 50][:5]
