"""Auto-tag suggestion using local LLM."""

from __future__ import annotations

import logging
import re

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a content tagger. Given an article title and text, "
    "suggest 1-3 tags that represent the broad topic categories. "
    "Rules:\n"
    "- Each tag must be a SINGLE word (no spaces, no hyphens)\n"
    "- Japanese tags: one word (e.g. AI、セキュリティ、政治)\n"
    "- English tags: one lowercase word (e.g. python, security, science)\n"
    "- Use Japanese for Japanese content, English for English content\n"
    "- Prefer broad categories over specific details\n"
    "- If existing tags are listed, reuse them when appropriate\n"
    "Return ONLY a comma-separated list, nothing else."
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
    result = await chat_completion(messages, max_tokens=40, temperature=0.1)
    if not result:
        return []
    tags = [t.strip().lower().strip('"\'') for t in re.split(r"[,\n]", result)]
    # スペースを含む複合タグは除外（1単語のみ）
    tags = [t for t in tags if t and " " not in t and len(t) < 30]
    return tags[:3]
