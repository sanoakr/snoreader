"""Combined article processor: summary + tag suggestions in a single LLM call.

Using a single call is required for the Ternary-Bonsai-8B model, which can only
generate tag pairs reliably when it continues from a SUMMARY section it just wrote.
"""

from __future__ import annotations

import hashlib
import logging
import re

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an article processor. Output a summary and topic tags for the given article.\n"
    "Output format (follow EXACTLY — no extra text):\n"
    "SUMMARY:\n"
    "・<bullet point in Japanese>\n"
    "・<bullet point in Japanese>\n"
    "TAGS: <english>|<日本語>, <english>|<日本語>\n\n"
    "Rules:\n"
    "- SUMMARY: 3-5 Japanese bullet points, key facts only, no opinions\n"
    "- SUMMARY bullets start with '・' and contain ONLY Japanese text — no English|Japanese pairs\n"
    "- TAGS: 1-3 broad topic tags; single lowercase English word (or hyphenated) + Japanese translation\n"
    "- If existing tags are provided, reuse them when appropriate\n"
    "- Return ONLY the formatted block above, nothing else"
)

# Rejects summary bullets containing tag-format annotations like "security|セキュリティ"
_TAG_IN_BULLET = re.compile(r"\b[a-z]{2,}\|")
# Valid English tag: starts with a-z, contains only a-z/0-9/hyphen, 1-29 total chars
_VALID_EN_TAG = re.compile(r"^[a-z][a-z0-9-]{0,28}$")


def _parse_output(raw: str) -> tuple[str | None, list[tuple[str, str | None]]]:
    """Parse combined LLM output into (summary, [(en, ja), ...])."""
    summary_lines: list[str] = []
    tags_str = ""
    in_summary = False

    for line in raw.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("SUMMARY:"):
            in_summary = True
        elif upper.startswith("TAGS:"):
            in_summary = False
            tags_str = stripped[5:].strip()
        elif in_summary and stripped.startswith("・"):
            # Only accept ・-prefixed lines; skip bullets with tag-format annotations
            if not _TAG_IN_BULLET.search(stripped):
                summary_lines.append(stripped)

    summary = "\n".join(summary_lines) if summary_lines else None

    pairs: list[tuple[str, str | None]] = []
    for item in re.split(r"[,\n]", tags_str):
        item = item.strip().strip("\"'")
        if not item:
            continue
        if "|" in item:
            en, ja = item.split("|", 1)
            en = en.strip().lower()
            ja = ja.strip() or None
        else:
            en = item.lower().strip()
            ja = None
        if _VALID_EN_TAG.match(en):
            pairs.append((en, ja))
    pairs = pairs[:3]

    return summary, pairs


async def summarize_and_tag(
    title: str,
    text: str,
    existing_tags: list[str] | None = None,
    priority: int | None = None,
) -> tuple[str | None, list[tuple[str, str | None]]]:
    """Generate summary and tag suggestions in a single LLM call.

    Returns (summary_text | None, [(en, ja), ...]).
    """
    existing_str = f"\nExisting tags: {', '.join(existing_tags)}" if existing_tags else ""
    # Per-article hash prefix breaks mlx-lm KV cache chain between consecutive articles
    uid = hashlib.md5(f"proc:{title}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Process only this article."
                f"{existing_str}\n\nTitle: {title}\n\n{text[:3000]}"
            ),
        },
    ]
    result = await chat_completion(messages, max_tokens=400, temperature=0.2, priority=priority)
    if not result:
        return None, []
    return _parse_output(result)
