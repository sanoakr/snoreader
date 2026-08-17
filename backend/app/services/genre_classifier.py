"""タグ候補から記事のジャンルを 1 つ決める分類器。

LLM は記事をまたいだ語彙の一貫性を保てない（実測で語彙外タグが出現回数の 73%）。
一括操作は「その束が該当記事を漏れなく含む」ことに依存するため、分類は
編集可能な辞書による決定的な写像で行う。分類そのものは DB に触らない純関数とし、
ルールは呼び出し側がスナップショットとして渡す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

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
    # 子 key -> 親 key。キーを持たないジャンルは親（またはトップレベル）扱い
    parent: dict[str, str] = field(default_factory=dict)

    def ancestors(self, genre: str) -> set[str]:
        """genre の祖先キー集合。階層は 2 段だが、循環しても止まるよう辿る。"""
        seen: set[str] = set()
        current = self.parent.get(genre)
        while current and current not in seen:
            seen.add(current)
            current = self.parent.get(current)
        return seen


def _prune_ancestors(hits: list[str], rules: GenreRules) -> list[str]:
    """候補の中に祖先と子孫が混在していたら祖先を落とす。

    「より具体的な指定が勝つ」という規則。これが無いと、親を指す代表タグ
    （ai など）を持つ記事が子ルールに当たっても親に残り、分割されない。
    """
    covered: set[str] = set()
    for genre in hits:
        covered |= rules.ancestors(genre)
    pruned = [g for g in hits if g not in covered]
    # 全滅（循環など異常なケース）のときは元の候補で解決する
    return pruned or hits


def _resolve(genres: list[str], rules: GenreRules) -> str:
    """候補ジャンルから priority 最小のものを返す。同値は key の辞書順で決める。"""
    candidates = _prune_ancestors(genres, rules)
    return min(candidates, key=lambda g: (rules.priority.get(g, _FALLBACK_PRIORITY), g))


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
    genre_rows = (await session.execute(select(Genre.id, Genre.key, Genre.parent_id))).all()
    key_by_id = {gid: key for gid, key, _parent in genre_rows}
    parent: dict[str, str] = {}
    for _gid, key, parent_id in genre_rows:
        parent_key = key_by_id.get(parent_id) if parent_id is not None else None
        if parent_key:
            parent[key] = parent_key

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
    # ルールを持たない親も priority を引けるようにする
    for _gid, key, _parent_id in genre_rows:
        priority.setdefault(key, _FALLBACK_PRIORITY)
    return GenreRules(tag_to_genre, generic_to_genre, priority, parent)


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
