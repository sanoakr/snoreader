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
from app.models import Article, Genre, GenreRule, GenreSplitSuggestion

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
            "projected_target": proposal.projected_target,
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
        projected_target=data["projected_target"],
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


async def refresh_split_suggestions(
    session: AsyncSession, *, priority: int | None = None
) -> int:
    """上限超のジャンルを検知し、新しい提案を保存して件数を返す。commit しない。

    再提案の規則:
    - 同じ (genre_key, strategy) の保留中（dismissed_at が None）の行があれば作らない
    - 無視済み（dismissed_at がある）の行しかない場合は、現在の未読件数が
      無視した時点の件数（dismissed_at_count）より増えたときだけ作る

    先に、上限を下回った（読まれた・保存された・非表示にされた等で減った）ジャンルの
    保留中の提案を失効させる（#4b）。そうしないと、利用者が既に読み下げたジャンルに
    対して UI が 47 秒かかる適用を提案し続けてしまう。失効した提案を再計算して
    差し替えることはしない——これは意図的に見送っている。単に消して、次にまた
    超過したら新しい提案が作られるのに任せる。

    priority はラベル命名の LLM 呼び出しの task_queue 優先度（省略時は
    PRIORITY_BACKGROUND）。スケジューラ発の呼び出しはユーザー操作を待たせないよう
    背景優先度のままにし、手動の再計算エンドポイントだけが前景優先度を渡す（#10）。
    """
    from collections import Counter
    from dataclasses import replace

    from app.ai.genre_namer import name_genres
    from app.services.genre_classifier import classify, load_rules
    from app.services.genre_split_planner import ProposedChild, SplitProposal, plan_splits

    articles = await _unread_articles(session)
    rules = await load_rules(session)
    counts = Counter(classify(tags, rules) for _aid, tags in articles)

    existing = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
    stale_ids = {
        row.id
        for row in existing
        if row.dismissed_at is None
        and counts.get(row.genre_key, 0) <= settings.genre_unread_limit
    }
    for row in existing:
        if row.id in stale_ids:
            await session.delete(row)
    existing = [row for row in existing if row.id not in stale_ids]

    if not articles:
        return 0

    proposals = plan_splits(articles, rules, limit=settings.genre_unread_limit)
    if not proposals:
        return 0

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
    labels = await name_genres(groups, priority=priority)
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


def _utcnow() -> str:
    """タイムスタンプ形式の単一の源は app.models._utcnow。ここでは複製しない。"""
    from app.models import _utcnow as models_utcnow

    return models_utcnow()


async def _close_pending_for_genre(session: AsyncSession, genre_key: str, count: int) -> int:
    """そのジャンルの保留中（dismissed_at が None）の提案を全部閉じる。閉じた行数を返す。

    dismiss 専用。「ユーザーが断った」という意味のフロアを立てる操作で、
    apply（辞書が変わって古くなっただけ）とは別の事象なので混同しない（#1）。
    """
    rows = (
        await session.execute(
            select(GenreSplitSuggestion).where(
                GenreSplitSuggestion.genre_key == genre_key,
                GenreSplitSuggestion.dismissed_at.is_(None),
            )
        )
    ).scalars().all()
    now = _utcnow()
    for row in rows:
        row.dismissed_at = now
        row.dismissed_at_count = count
    return len(rows)


async def _delete_all_pending(session: AsyncSession) -> int:
    """保留中（dismissed_at が None）の提案を、ジャンルを問わず全部削除する。

    apply 専用（#1, #4a）。辞書が変わった時点で、対象ジャンルだけでなく
    全ジャンルの projected が無効になる（demote_generic は他ジャンルへ再配分
    するので、対象以外の保留提案の projected も同時に古くなる）。ユーザーが
    「断った」わけではないので、dismiss のようにフロアを立てて残す意味がない
    ——単に消す。消した行数を返す。
    """
    rows = (
        await session.execute(
            select(GenreSplitSuggestion).where(GenreSplitSuggestion.dismissed_at.is_(None))
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    return len(rows)


async def apply_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    *,
    labels: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """分割提案を適用する。(created, moved, reclassified) を返す。commit は呼び出し側。

    - demote_tags は GenreRule.is_generic = True にする
    - children は Genre を作り（無ければ）、タグの GenreRule を付け替える
    - 最後に reclassify_all を 1 回だけ呼ぶ（本番で ~47 秒かかるので、この経路だけに限る）
    - 辞書が変わった後は全ジャンルの projected_max が無効になるので、ジャンルを
      問わず保留中の提案を全部削除する（#1, #4a。dismiss のようにフロアを
      立てるのではない——「断られた」わけではないので単に消す）

    提案の保存後、DB がスナップショットと食い違っていることがある
    （対象ジャンルが消えた、demote_tags が別ジャンルへ動いた、または子キーと
    同名の別ジャンルが独立に作られた）。そういう食い違いは検証フェーズで
    洗い出し、何も変更する前に例外を投げる。「見つからない」（LookupError）と
    「見つかったが古いスナップショットと矛盾する」（ValueError）は呼び出し側が
    別々に扱えるよう区別する。
    """
    from app.services.genre_classifier import OTHER_GENRE, reclassify_all
    from app.services.genre_split_planner import DEFAULT_NEW_GENRE_PRIORITY

    row = await session.get(GenreSplitSuggestion, suggestion_id)
    if row is None:
        raise LookupError("Suggestion not found")
    proposal = payload_to_proposal(row.payload)
    overrides = labels or {}

    # --- 検証フェーズ: まだ何も変更していない。ここで例外を投げれば
    # 部分適用（デモート済みなのに子は作られていない、等）を残さない ---
    is_other = proposal.genre_key == OTHER_GENRE
    parent_id: int | None = None
    priority = DEFAULT_NEW_GENRE_PRIORITY

    target: Genre | None = None
    if not is_other:
        # children の有無に関係なく必ず対象ジャンルの存在を確認する（#5）。
        # 以前は children が空（demote_generic）だとこのブロック自体を
        # スキップしていたため、対象ジャンルが削除されていても検出できなかった
        target = (
            await session.execute(select(Genre).where(Genre.key == proposal.genre_key))
        ).scalar_one_or_none()
        if target is None:
            raise LookupError("Genre no longer exists")

    if proposal.children and not is_other:
        # target が子なら新しい子は兄弟（同じ親）に、target が子を持たない
        # トップレベルなら target 自身の子にする。階層は 2 段のまま
        parent_id = target.parent_id if target.parent_id is not None else target.id
        parent = await session.get(Genre, parent_id)
        if parent is not None and parent.parent_id is not None:
            # ここに到達するのは DB の不変条件（階層は 2 段まで）が既に破れて
            # いるときだけのはずだが、将来の回帰が静かに 3 段目を作るより、
            # ここで大きく落ちてほしい（#6）
            raise AssertionError(
                f"Refusing to nest under '{parent.key}', which is itself a child; "
                "genres can only nest one level deep"
            )
        priority = parent.priority if parent is not None else target.priority

    for child in proposal.children:
        existing_genre = (
            await session.execute(select(Genre).where(Genre.key == child.key))
        ).scalar_one_or_none()
        if existing_genre is not None and existing_genre.parent_id != parent_id:
            # この key を持つジャンルは既にあるが、意図した親と食い違う。
            # 提案の保存後に利用者が独立にそのキーのジャンルを作った/動かした
            # ということなので、黙って書き換えて乗っ取ると利用者のジャンルが
            # 気付かれずに別物になる。何も変更せず失敗させ、再提案を促す
            raise ValueError(
                f"Genre '{child.key}' already exists with a different parent; "
                "the proposal is stale — refresh split suggestions and retry"
            )

    if proposal.demote_tags and target is not None:
        # demote_generic のスナップショットが今も有効か検証する（#5）。保存後に
        # 利用者がそのタグを別ジャンルへ動かしていたら、黙って別ジャンルの
        # ルールを降格してしまう——child-key の衝突ガードと対になる検証
        for tag in proposal.demote_tags:
            rule = (
                await session.execute(select(GenreRule).where(GenreRule.tag == tag))
            ).scalar_one_or_none()
            if rule is not None and rule.genre_id != target.id:
                raise ValueError(
                    f"Tag '{tag}' no longer belongs to '{proposal.genre_key}'; "
                    "the proposal is stale — refresh split suggestions and retry"
                )

    # --- ここから変更フェーズ ---
    created = 0
    moved = 0

    # 受け皿タグの汎用降格（demote_generic 戦略）
    for tag in proposal.demote_tags:
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == tag))
        ).scalar_one_or_none()
        if rule is not None and not rule.is_generic:
            rule.is_generic = True
            moved += 1

    # 新しい子（other 由来なら新トップレベル）を作り、タグを付け替える
    for child in proposal.children:
        genre = (
            await session.execute(select(Genre).where(Genre.key == child.key))
        ).scalar_one_or_none()
        label = overrides.get(child.key, child.label_ja)
        if genre is None:
            genre = Genre(
                key=child.key,
                label_ja=label,
                priority=priority,
                parent_id=parent_id,
            )
            session.add(genre)
            await session.flush()
            created += 1
        else:
            genre.label_ja = label

        for tag in child.tags:
            rule = (
                await session.execute(select(GenreRule).where(GenreRule.tag == tag))
            ).scalar_one_or_none()
            if rule is None:
                # 既存 POST /genre-rules と同じ「衝突ではなく付け替え」の作法
                session.add(GenreRule(tag=tag, genre_id=genre.id, is_generic=False))
                moved += 1
            elif rule.genre_id != genre.id:
                rule.genre_id = genre.id
                # _simulate の tag_moves は移動先タグを常に tag_to_genre
                # （通常段）に置いて件数を測っているので、既存ルールが
                # is_generic=True のまま付け替わると通常段より優先度の低い
                # 汎用段に落ちてしまい、reclassify_all の結果が利用者が
                # 承認した件数と食い違う。既存の genre_seed.seed_subgenres は
                # タグを子へ移すときに is_generic を保つ（technology は汎用の
                # まま子へ移る、というシード投入の作法としては正しい）が、
                # ここは別の話（提案の想定に合わせる）なので明示的に False にする
                rule.is_generic = False
                moved += 1

    await session.flush()
    reclassified = await reclassify_all(session)
    # 辞書が変わったので、対象ジャンルだけでなく全ジャンルの保留中の提案（この
    # 提案自身も含む）が無効になる。dismiss と違い「断られた」わけではないので、
    # フロアを立てず単に消す（#1, #4a）
    await _delete_all_pending(session)
    return created, moved, reclassified


async def dismiss_suggestion(session: AsyncSession, suggestion_id: int) -> int:
    """提案を無視する。同じ genre_key の保留中の他の案も全部閉じる。

    閉じた行数を返す。commit は呼び出し側。
    """
    row = await session.get(GenreSplitSuggestion, suggestion_id)
    if row is None:
        raise LookupError("Suggestion not found")
    return await _close_pending_for_genre(session, row.genre_key, row.before_count)
