"""タグ候補から記事のジャンルを 1 つ決める分類器。

LLM は記事をまたいだ語彙の一貫性を保てない（実測で語彙外タグが出現回数の 73%）。
一括操作は「その束が該当記事を漏れなく含む」ことに依存するため、分類は
編集可能な辞書による決定的な写像で行う。分類そのものは DB に触らない純関数とし、
ルールは呼び出し側がスナップショットとして渡す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Genre, GenreRule

# どのルールにも当たらなかった記事の受け皿。genres テーブルに行は持たない予約キー
OTHER_GENRE = "other"

# priority が未登録のジャンルは最も低い優先度として扱う
_FALLBACK_PRIORITY = 1_000_000

# reclassify_all の一括 UPDATE バッチサイズ（main.py _backfill_normalized_urls に合わせる）
_RECLASSIFY_BATCH_SIZE = 1000


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

    LLM は呼ばないが、本番相当の件数（3000 件弱）では数十秒かかる
    （実測 47 秒）。`content` を含む全列を ORM で読むと 1 記事あたり数十KB
    (本番で content 計 91MB) を無駄に読み込むため、必要な 3 列だけ SELECT し、
    変化があった行だけ `_backfill_normalized_urls`（main.py）と同じ型で
    バッチ UPDATE する。commit は呼び出し側が行う。
    """
    rules = await load_rules(session)
    rows = (
        await session.execute(
            select(Article.id, Article.tag_suggestions, Article.genre).where(
                Article.tag_suggestions.isnot(None)
            )
        )
    ).all()

    updates = []
    for article_id, tag_suggestions, current_genre in rows:
        genre = classify(parse_tags(tag_suggestions), rules)
        if genre != current_genre:
            updates.append({"id": article_id, "genre": genre})

    for i in range(0, len(updates), _RECLASSIFY_BATCH_SIZE):
        await session.execute(update(Article), updates[i : i + _RECLASSIFY_BATCH_SIZE])
    if updates:
        await session.flush()
    return len(updates)
