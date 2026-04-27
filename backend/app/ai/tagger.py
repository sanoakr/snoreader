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
    "- Always use English as the primary tag (single lowercase word, no spaces)\n"
    "- Also provide a Japanese translation for each tag\n"
    "- Format: english|日本語 for each tag, comma-separated\n"
    "- Examples: ai|AI, security|セキュリティ, python|Python, politics|政治\n"
    "- Use broad categories, not specific details\n"
    "- If existing tags are listed, reuse them when appropriate\n"
    "Return ONLY the comma-separated pairs, nothing else."
)


def _parse_tag_pairs(raw: str) -> list[tuple[str, str | None]]:
    """Parse 'en|ja,en|ja,...' into list of (en, ja) tuples."""
    pairs = []
    for item in re.split(r"[,\n]", raw):
        item = item.strip().strip('"\'')
        if not item:
            continue
        if "|" in item:
            parts = item.split("|", 1)
            en = parts[0].strip().lower()
            ja = parts[1].strip() or None
        else:
            en = item.lower()
            ja = None
        if en and " " not in en and len(en) < 30:
            pairs.append((en, ja))
    return pairs[:3]


_TRANSLATE_SYSTEM = (
    "Translate English tech tags to Japanese. "
    "Rules:\n"
    "- Use katakana for foreign loanwords (e.g. coding→コーディング, security→セキュリティ)\n"
    "- Keep proper nouns as-is or in katakana (Python→Python, GitHub→GitHub, AI→AI)\n"
    "- Use common Japanese tech terms when they exist (programming→プログラミング)\n"
    "- Format: english=日本語 per line\n"
    "Return ONLY the translation pairs, one per line, nothing else."
)


_STATIC_JA: dict[str, str] = {
    "ai": "AI", "automation": "自動化", "code": "コード", "coding": "コーディング",
    "security": "セキュリティ", "programming": "プログラミング", "education": "教育",
    "free": "無料", "japan": "日本", "japanese": "日本語", "terminal": "ターミナル",
    "troubleshooting": "トラブルシューティング", "devtools": "開発ツール",
    "llmvision": "LLMビジョン", "politics": "政治", "science": "科学",
    "health": "健康", "technology": "技術", "business": "ビジネス",
    "research": "研究", "news": "ニュース", "society": "社会",
    "environment": "環境", "economics": "経済", "culture": "文化",
}


async def translate_tags(names: list[str]) -> dict[str, str]:
    """Translate English tag names to Japanese using LLM with static fallback.

    Returns dict of {en_name: ja_name}.
    """
    if not names:
        return {}
    # 静的マッピングで賄えるものは先に解決
    out = {n: _STATIC_JA[n] for n in names if n in _STATIC_JA}
    remaining = [n for n in names if n not in out]
    if not remaining:
        return out

    messages = [
        {"role": "system", "content": _TRANSLATE_SYSTEM},
        {"role": "user", "content": "\n".join(remaining)},
    ]
    max_tokens = min(len(remaining) * 20, 400)
    result = await chat_completion(messages, max_tokens=max_tokens, temperature=0.1)
    if result:
        for line in result.splitlines():
            line = line.strip()
            if "=" in line:
                en, ja = line.split("=", 1)
                en = en.strip().lower()
                ja = ja.strip()
                if en and ja and en in remaining:
                    out[en] = ja
    return out


async def suggest_tags(
    title: str,
    text: str,
    existing_tags: list[str] | None = None,
) -> list[tuple[str, str | None]]:
    """Suggest tags for an article.

    Returns list of (name_en, name_ja) tuples. Empty list if LLM unavailable.
    """
    content = text[:2000]
    user_parts = []
    if existing_tags:
        user_parts.append(f"Existing tags: {', '.join(existing_tags)}")
    user_parts.append(f"Title: {title}\n\n{content}")
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]
    result = await chat_completion(messages, max_tokens=60, temperature=0.1)
    if not result:
        return []
    return _parse_tag_pairs(result)
