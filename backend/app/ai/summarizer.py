"""Article summarization using local LLM."""

from __future__ import annotations

import logging

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise article summarizer. "
    "ALWAYS respond in Japanese, regardless of the article's language. "
    "Summarize as 3-5 bullet points, each starting with '・'. "
    "Focus on key facts and takeaways. Do not add opinions. "
    "Return ONLY the bullet points in Japanese, nothing else."
)


async def summarize_article(title: str, text: str) -> str | None:
    """Generate a summary for an article. Returns None if LLM is unavailable."""
    content = text[:3000]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Summarize only this article, ignoring any previous context.\n\nTitle: {title}\n\n{content}"},
    ]
    return await chat_completion(messages, max_tokens=256, temperature=0.2)
