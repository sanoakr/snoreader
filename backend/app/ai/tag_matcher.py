"""Match existing tags against article title/body via substring/word-boundary."""

from __future__ import annotations

import re

from app.models import Tag


def _match(keyword: str, haystack: str) -> bool:
    """Case-insensitive match.

    短い ASCII キーワード (<=4 chars) は word boundary を要求して誤爆を防ぐ
    (e.g. 'ai' が 'said' にマッチするのを回避)。それ以外はシンプルな部分一致。
    """
    kw = keyword.lower()
    hay = haystack.lower()
    if kw.isascii() and len(kw) <= 4:
        return re.search(rf"\b{re.escape(kw)}\b", hay) is not None
    return kw in hay


def match_existing_tags(
    all_tags: list[Tag],
    title: str,
    text: str,
    *,
    max_results: int = 10,
    body_limit: int = 1500,
) -> list[Tag]:
    """Return existing tags whose English or Japanese name appears in title/body.

    タイトル一致を優先し、次に本文一致。タグの並び順は入力の走査順を保つ。
    """
    body_slice = (text or "")[:body_limit]
    title_src = title or ""

    title_hits: list[Tag] = []
    body_hits: list[Tag] = []

    for tag in all_tags:
        names = [tag.name]
        if tag.name_ja:
            names.append(tag.name_ja)
        if any(_match(n, title_src) for n in names):
            title_hits.append(tag)
            continue
        if any(_match(n, body_slice) for n in names):
            body_hits.append(tag)

    return (title_hits + body_hits)[:max_results]
