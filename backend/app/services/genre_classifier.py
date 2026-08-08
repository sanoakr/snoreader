"""タグ候補から記事のジャンルを 1 つ決める分類器。

LLM は記事をまたいだ語彙の一貫性を保てない（実測で語彙外タグが出現回数の 73%）。
一括操作は「その束が該当記事を漏れなく含む」ことに依存するため、分類は
編集可能な辞書による決定的な写像で行う。分類そのものは DB に触らない純関数とし、
ルールは呼び出し側がスナップショットとして渡す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Genre, GenreRule

# どのルールにも当たらなかった記事の受け皿。genres テーブルに行は持たない予約キー
OTHER_GENRE = "other"

# priority が未登録のジャンルは最も低い優先度として扱う
_FALLBACK_PRIORITY = 1_000_000


@dataclass(frozen=True)
class GenreRules:
    """DB から組み立てた分類ルールのスナップショット。"""

    tag_to_genre: dict[str, str]      # 通常ルール: tag -> genre key
    generic_to_genre: dict[str, str]  # 汎用ルール: tag -> genre key
    priority: dict[str, int]          # genre key -> priority（小さいほど優先）


def _resolve(genres: list[str], rules: GenreRules) -> str:
    """候補ジャンルから priority 最小のものを返す。同値は key の辞書順で決める。"""
    return min(genres, key=lambda g: (rules.priority.get(g, _FALLBACK_PRIORITY), g))


def classify(tags: list[str], rules: GenreRules) -> str:
    """タグ候補からジャンルを 1 つ決める。該当なしは "other"。"""
    hits = [rules.tag_to_genre[t] for t in tags if t in rules.tag_to_genre]
    if hits:
        return _resolve(hits, rules)

    generic_hits = [rules.generic_to_genre[t] for t in tags if t in rules.generic_to_genre]
    if generic_hits:
        return _resolve(generic_hits, rules)

    return OTHER_GENRE


async def load_rules(session: AsyncSession) -> GenreRules:
    """genres / genre_rules から分類ルールのスナップショットを組み立てる。

    ルール表は 150 行程度と小さいのでキャッシュは持たない。多数の記事を回す
    ときだけ、呼び出し側がループの外で 1 回呼ぶこと。
    """
    rows = (
        await session.execute(
            select(GenreRule.tag, GenreRule.is_generic, Genre.key, Genre.priority).join(
                Genre, GenreRule.genre_id == Genre.id
            )
        )
    ).all()

    tag_to_genre: dict[str, str] = {}
    generic_to_genre: dict[str, str] = {}
    priority: dict[str, int] = {}
    for tag, is_generic, key, prio in rows:
        (generic_to_genre if is_generic else tag_to_genre)[tag] = key
        priority[key] = prio
    return GenreRules(tag_to_genre, generic_to_genre, priority)


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(tags, list):
        # 構文としては妥当だが list ではない JSON（null / 数値 / dict など）を弾く
        return []
    return [t for t in tags if isinstance(t, str)]


async def reclassify_all(session: AsyncSession) -> int:
    """tag_suggestions を持つ全記事を分類し直し、変化した件数を返す。

    LLM を呼ばないので数千件でも一瞬で終わる。commit は呼び出し側が行う。
    """
    rules = await load_rules(session)
    articles = (
        await session.execute(select(Article).where(Article.tag_suggestions.isnot(None)))
    ).scalars().all()

    changed = 0
    for article in articles:
        genre = classify(parse_tags(article.tag_suggestions), rules)
        if article.genre != genre:
            article.genre = genre
            changed += 1
    if changed:
        await session.flush()
    return changed
