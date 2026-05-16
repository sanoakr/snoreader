"""Article summarization using local LLM."""

from __future__ import annotations

import hashlib
import logging

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise article summarizer. "
    "ALWAYS respond in Japanese, regardless of the article's language. "
    "Summarize as EXACTLY 3 bullet points, each starting with '・'. "
    "Focus on key facts and takeaways. Do not add opinions. "
    "Return ONLY the 3 bullet points in Japanese. "
    "Do NOT output any English tags, labels, or 'word|translation' pairs. "
    "Do NOT output section headers like 'SUMMARY:' or 'TAGS:'. "
    "Output ONLY exactly 3 Japanese bullet points starting with '・', nothing else."
)


def _clean_summary(raw: str) -> str | None:
    """Extract only ・-prefixed lines from LLM output, capped at exactly 3."""
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip().startswith("・")
    ]
    lines = lines[:3]
    return "\n".join(lines) if lines else None


async def summarize_article(title: str, text: str, priority: int | None = None) -> str | None:
    """Generate a summary for an article. Returns None if LLM is unavailable."""
    content = text[:3000]
    # Unique per-article hash prefix prevents mlx-lm KV cache reuse across articles
    uid = hashlib.md5(f"sum:{title}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Summarize only this article.\n\n"
                f"Title: {title}\n\n{content}"
            ),
        },
    ]
    raw = await chat_completion(messages, max_tokens=256, temperature=0.2, priority=priority)
    if not raw:
        return None
    return _clean_summary(raw)
