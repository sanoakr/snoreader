"""分割提案の保存・適用・無視。DB 側の入口。

分割の計算そのものは genre_split_planner（DB に触らない純関数）が行う。
ここは未読の集計、LLM によるラベル命名、提案の upsert を担う。
commit は呼び出し側が行う（既存 seed_subgenres と同じ作法）。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Article, GenreSplitSuggestion

if TYPE_CHECKING:
    # 型注釈だけのための遅延インポート（実行時には読み込まない・app.services.* の
    # 遅延インポート規約に合わせる）
    from app.services.genre_split_planner import SplitProposal


def proposal_to_payload(proposal: "SplitProposal") -> str:
    """SplitProposal を JSON 文字列にする。"""
    return json.dumps(
        {
            "genre_key": proposal.genre_key,
            "strategy": proposal.strategy,
            "before": proposal.before,
            "projected_max": proposal.projected_max,
            "children": [
                {
                    "key": c.key,
                    "label_ja": c.label_ja,
                    "tags": list(c.tags),
                    "estimated_unread": c.estimated_unread,
                }
                for c in proposal.children
            ],
            "demote_tags": list(proposal.demote_tags),
        },
        ensure_ascii=False,
    )


def payload_to_proposal(payload: str) -> "SplitProposal":
    """JSON 文字列から SplitProposal を復元する。"""
    from app.services.genre_split_planner import ProposedChild, SplitProposal

    data = json.loads(payload)
    return SplitProposal(
        genre_key=data["genre_key"],
        strategy=data["strategy"],
        before=data["before"],
        projected_max=data["projected_max"],
        children=tuple(
            ProposedChild(
                key=c["key"],
                label_ja=c["label_ja"],
                tags=tuple(c["tags"]),
                estimated_unread=c["estimated_unread"],
            )
            for c in data["children"]
        ),
        demote_tags=tuple(data["demote_tags"]),
    )


async def _unread_articles(session: AsyncSession) -> list[tuple[int, list[str]]]:
    """未読・未保存・未非表示の記事の (id, タグ) を返す。

    列は 2 つだけ読む（content を含む全列を ORM で読むと本番で 91MB になる）。
    """
    from app.services.genre_classifier import parse_tags

    rows = (
        await session.execute(
            select(Article.id, Article.tag_suggestions).where(
                Article.is_read == False,  # noqa: E712
                Article.is_saved == False,  # noqa: E712
                Article.dismissed_at.is_(None),
                Article.tag_suggestions.isnot(None),
            )
        )
    ).all()
    return [(aid, parse_tags(raw)) for aid, raw in rows]


async def refresh_split_suggestions(session: AsyncSession) -> int:
    """上限超のジャンルを検知し、新しい提案を保存して件数を返す。commit しない。

    再提案の規則:
    - 同じ (genre_key, strategy) の保留中（dismissed_at が None）の行があれば作らない
    - 無視済み（dismissed_at がある）の行しかない場合は、現在の未読件数が
      無視した時点の件数（dismissed_at_count）より増えたときだけ作る
    """
    from dataclasses import replace

    from app.ai.genre_namer import name_genres
    from app.services.genre_classifier import load_rules
    from app.services.genre_split_planner import ProposedChild, SplitProposal, plan_splits

    articles = await _unread_articles(session)
    if not articles:
        return 0
    rules = await load_rules(session)
    proposals = plan_splits(articles, rules, limit=settings.genre_unread_limit)
    if not proposals:
        return 0

    existing = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
    pending = {(r.genre_key, r.strategy) for r in existing if r.dismissed_at is None}
    # 無視済みは、そのときの件数より増えたら再提案する
    dismissed_floor: dict[tuple[str, str], int] = {}
    for row in existing:
        if row.dismissed_at is None:
            continue
        key = (row.genre_key, row.strategy)
        floor = row.dismissed_at_count if row.dismissed_at_count is not None else row.before_count
        dismissed_floor[key] = max(dismissed_floor.get(key, 0), floor)

    fresh: list[SplitProposal] = []
    for proposal in proposals:
        key = (proposal.genre_key, proposal.strategy)
        if key in pending:
            continue
        if proposal.before <= dismissed_floor.get(key, 0):
            continue
        fresh.append(proposal)
    if not fresh:
        return 0

    # ラベル命名は LLM を 1 回だけ。子を持たない案（demote_generic）は tags が
    # 空なのでグループが 0 件になり、name_genres は空リストを渡されても長さ 0 を返す
    groups = [c.tags for p in fresh for c in p.children]
    labels = await name_genres(groups)
    named = iter(labels)

    # dataclasses.replace で凍結データクラス（ProposedChild / SplitProposal）の
    # 再ラベル版を作る（type(x)(...) は構築対象が読みにくいので使わない）
    for proposal in fresh:
        children: tuple[ProposedChild, ...] = tuple(
            replace(c, label_ja=next(named, c.label_ja)) for c in proposal.children
        )
        stored: SplitProposal = replace(proposal, children=children)
        session.add(
            GenreSplitSuggestion(
                genre_key=stored.genre_key,
                strategy=stored.strategy,
                payload=proposal_to_payload(stored),
                before_count=stored.before,
                projected_max=stored.projected_max,
            )
        )
    await session.flush()
    return len(fresh)
