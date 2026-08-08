"""タグ候補から記事のジャンルを 1 つ決める分類器。

LLM は記事をまたいだ語彙の一貫性を保てない（実測で語彙外タグが出現回数の 73%）。
一括操作は「その束が該当記事を漏れなく含む」ことに依存するため、分類は
編集可能な辞書による決定的な写像で行う。分類そのものは DB に触らない純関数とし、
ルールは呼び出し側がスナップショットとして渡す。
"""

from __future__ import annotations

from dataclasses import dataclass

# どのルールにも当たらなかった記事の受け皿。genres テーブルに行は持たない予約キー
OTHER_GENRE = "other"


@dataclass(frozen=True)
class GenreRules:
    """DB から組み立てた分類ルールのスナップショット。"""

    tag_to_genre: dict[str, str]      # 通常ルール: tag -> genre key
    generic_to_genre: dict[str, str]  # 汎用ルール: tag -> genre key
    priority: dict[str, int]          # genre key -> priority（小さいほど優先）


def _resolve(genres: list[str], rules: GenreRules) -> str:
    """候補ジャンルから priority 最小のものを返す。同値は key の辞書順で決める。"""
    return min(genres, key=lambda g: (rules.priority.get(g, 1_000_000), g))


def classify(tags: list[str], rules: GenreRules) -> str:
    """タグ候補からジャンルを 1 つ決める。該当なしは "other"。"""
    hits = [rules.tag_to_genre[t] for t in tags if t in rules.tag_to_genre]
    if hits:
        return _resolve(hits, rules)

    generic_hits = [rules.generic_to_genre[t] for t in tags if t in rules.generic_to_genre]
    if generic_hits:
        return _resolve(generic_hits, rules)

    return OTHER_GENRE
