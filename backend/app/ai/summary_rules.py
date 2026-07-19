"""Shared rules and bullet-list post-processing for AI-generated article summaries."""

from __future__ import annotations

import re

MIN_SUMMARY_BULLETS = 1
MAX_SUMMARY_BULLETS = 9

SUMMARY_RULES = (
    f"- SUMMARY: {MIN_SUMMARY_BULLETS}-{MAX_SUMMARY_BULLETS} Japanese bullet points — "
    "use as FEW as possible, only add a bullet if it conveys genuinely new information\n"
    "- Do NOT restate information already given in the title — every bullet must add "
    "something the title doesn't already say\n"
    "- Always include the article's conclusion, result, or outcome, so the title and "
    "summary together give a complete understanding of the article\n"
    "- SUMMARY bullets start with '・' and contain ONLY Japanese text — no English|Japanese pairs\n"
    "- Focus on key facts and takeaways only. Do not add opinions."
)

# Rejects summary bullets containing tag-format annotations like "security|セキュリティ"
_TAG_IN_BULLET = re.compile(r"\b[a-z]{2,}\|")


def finalize_bullets(lines: list[str]) -> str | None:
    """Drop tag-annotated lines, cap at MAX_SUMMARY_BULLETS, and join.

    `lines` must already be stripped, ・-prefixed bullet lines. Returns None
    if no valid bullet lines remain (treated as an LLM formatting failure).
    """
    cleaned = [line for line in lines if not _TAG_IN_BULLET.search(line)]
    cleaned = cleaned[:MAX_SUMMARY_BULLETS]
    return "\n".join(cleaned) if cleaned else None
