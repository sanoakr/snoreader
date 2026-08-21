"""未読が上限を超えた葉ジャンルの分割案を作る。

DB には触らない純関数。分類は既存 genre_classifier.classify を使い、
候補ルールに対して未読記事全件を実際に分類し直してから件数を出す
（推測値は出さない）。兄弟ジャンルは親と同じ priority を持つため
同順位になり、_resolve の同値解決（キーの辞書順）で決まる——
受け皿より後にソートされるキーの新兄弟は記事を 1 件も取れない。
シミュレーションを通せばこの失敗は projected 件数 0 として自動検出される。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from app.services.genre_classifier import OTHER_GENRE, GenreRules, classify

# 下限なしだと 2 件のジャンルが量産される（実データで未ルール共起タグは waymo 2 / google 2 級）
_MIN_CHILD_ARTICLES = 8
# 1 提案で辞書が大きく動きすぎないようにする
_MAX_NEW_CHILDREN = 4
# 貪欲詰めの 1 ビンあたり上限（limit のこの割合まで）。分割直後に再超過しないための余裕
_BIN_FILL_RATIO = 0.8


@dataclass(frozen=True)
class ProposedChild:
    key: str
    label_ja: str
    tags: tuple[str, ...]
    estimated_unread: int


@dataclass(frozen=True)
class SplitProposal:
    genre_key: str
    strategy: str
    before: int
    projected_max: int
    children: tuple[ProposedChild, ...]
    demote_tags: tuple[str, ...] = ()


def plan_splits(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> list[SplitProposal]:
    """上限を超えた葉ジャンルごとに、成立した分割案を全部返す。

    articles は (article_id, tags) の列。rules のスナップショットで分類した
    結果が上限を超えたジャンルだけを対象にする。
    """
    return []
