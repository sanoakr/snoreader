"""ジャンル定義の CRUD。

粒度と分け方は運用しながら変わるので、辞書はコード定数ではなく DB に置く。
変更のたびにその場で既存記事を再分類する（POST /exclude-patterns が追加時に
既存記事を purge するのと同じ作法）。LLM を呼ばないので数千件でも一瞬。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models import Genre, GenreRule, GenreSplitSuggestion
from app.schemas import (
    ApplySuggestionBody,
    ApplySuggestionResult,
    GenreCreate,
    GenreOut,
    GenreRuleCreate,
    GenreRuleOut,
    GenreUpdate,
    ProposedChildOut,
    ReclassifyResult,
    SeedSubgenresResult,
    SplitSuggestionOut,
)

router = APIRouter(tags=["genres"])

# genres テーブルに行を持たない予約キー（どのルールにも当たらない記事の受け皿）
_RESERVED_KEYS = {"other"}


async def _reclassify(session: AsyncSession) -> int:
    from app.services.genre_classifier import reclassify_all

    changed = await reclassify_all(session)
    await session.commit()
    return changed


async def _validate_parent(
    session: AsyncSession, parent_id: int | None, *, moving_id: int | None = None
) -> None:
    """親指定の妥当性を見る。階層は 2 段固定。"""
    if parent_id is None:
        return
    parent = await session.get(Genre, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent genre not found")
    if parent.parent_id is not None:
        # 親自身がすでに子なら、それを親にするのは 3 段目を作ることになる
        raise HTTPException(status_code=400, detail="Genres can only nest one level deep")
    if moving_id is not None and parent_id == moving_id:
        # 自分自身を親にすると自己参照の循環になる
        raise HTTPException(status_code=400, detail="A genre cannot be its own parent")
    if moving_id is not None:
        child_ids = {
            gid
            for (gid,) in (
                await session.execute(select(Genre.id).where(Genre.parent_id == moving_id))
            ).all()
        }
        # 自分の子を親にすると循環する
        if parent_id in child_ids:
            raise HTTPException(status_code=400, detail="A genre cannot be its own parent")
        # 子を持つジャンルを子にすると 3 段になる。親自身が top-level かを見る
        # だけでは塞げない: A(親) → B(子) を作った後で A を C の子にする、という
        # 順序で C → A → B が作れてしまう
        if child_ids:
            raise HTTPException(
                status_code=400, detail="A genre with children cannot become a child"
            )


async def _list_genres(session: AsyncSession) -> list[GenreOut]:
    genres = (
        await session.execute(select(Genre).order_by(Genre.priority, Genre.key))
    ).scalars().all()
    rules = (await session.execute(select(GenreRule))).scalars().all()

    by_genre: dict[int, tuple[list[GenreRuleOut], list[GenreRuleOut]]] = {
        g.id: ([], []) for g in genres
    }
    for rule in rules:
        normal, generic = by_genre.setdefault(rule.genre_id, ([], []))
        (generic if rule.is_generic else normal).append(GenreRuleOut(id=rule.id, tag=rule.tag))

    out: list[GenreOut] = []
    for genre in genres:
        normal, generic = by_genre[genre.id]
        out.append(
            GenreOut(
                id=genre.id,
                key=genre.key,
                label_ja=genre.label_ja,
                priority=genre.priority,
                parent_id=genre.parent_id,
                rules=sorted(normal, key=lambda r: r.tag),
                generic_rules=sorted(generic, key=lambda r: r.tag),
            )
        )
    return out


@router.get("/genres", response_model=list[GenreOut])
async def list_genres(session: AsyncSession = Depends(get_session)):
    return await _list_genres(session)


@router.post("/genres", response_model=GenreOut, status_code=201)
async def create_genre(body: GenreCreate, session: AsyncSession = Depends(get_session)):
    key = body.key.strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="Key must not be empty")
    if key in _RESERVED_KEYS:
        raise HTTPException(status_code=400, detail=f"'{key}' is a reserved key")

    existing = await session.execute(select(Genre).where(Genre.key == key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Genre already exists")

    await _validate_parent(session, body.parent_id)

    genre = Genre(
        key=key, label_ja=body.label_ja.strip(), priority=body.priority, parent_id=body.parent_id
    )
    session.add(genre)
    await session.commit()
    await session.refresh(genre)
    # 新規ジャンルにはまだルールが無いので実質 0 件だが、変更系レスポンスの契約は揃える
    changed = await _reclassify(session)
    return GenreOut(
        id=genre.id,
        key=genre.key,
        label_ja=genre.label_ja,
        priority=genre.priority,
        parent_id=genre.parent_id,
        reclassified=changed,
    )


# --- 分割提案（split-suggestions）---
#
# パス変数を持つ `/genres/{genre_id}` より前に置く。そうしないと FastAPI が
# "split-suggestions" を genre_id として解釈しようとして 422 になる。


@router.get("/genres/split-suggestions", response_model=list[SplitSuggestionOut])
async def list_split_suggestions(session: AsyncSession = Depends(get_session)):
    """保留中（未適用・未無視）の分割提案を projected_max 昇順で返す。"""
    from app.services.genre_split_store import payload_to_proposal

    rows = (
        await session.execute(
            select(GenreSplitSuggestion)
            .where(GenreSplitSuggestion.dismissed_at.is_(None))
            .order_by(GenreSplitSuggestion.projected_max.asc(), GenreSplitSuggestion.id.asc())
        )
    ).scalars().all()

    out: list[SplitSuggestionOut] = []
    for row in rows:
        proposal = payload_to_proposal(row.payload)
        out.append(
            SplitSuggestionOut(
                id=row.id,
                genre_key=row.genre_key,
                strategy=row.strategy,
                before=row.before_count,
                projected_max=row.projected_max,
                projected_target=proposal.projected_target,
                children=[
                    ProposedChildOut(
                        key=c.key,
                        label_ja=c.label_ja,
                        tags=list(c.tags),
                        estimated_unread=c.estimated_unread,
                    )
                    for c in proposal.children
                ],
                demote_tags=list(proposal.demote_tags),
                created_at=row.created_at,
                limit=settings.genre_unread_limit,
            )
        )
    return out


@router.post("/genres/split-suggestions/refresh", response_model=dict)
async def refresh_split_suggestions_endpoint(session: AsyncSession = Depends(get_session)):
    """手動で再計算する。通常はフィード取得サイクルの末尾で走る。

    ユーザーがボタンを押した操作なので、ラベル命名の LLM 呼び出しは前景優先度で
    行う（スケジューラ発の呼び出しはユーザー操作を待たせないよう背景優先度のまま。
    #10）。
    """
    from app.ai.task_queue import PRIORITY_FOREGROUND
    from app.services.genre_split_store import refresh_split_suggestions

    created = await refresh_split_suggestions(session, priority=PRIORITY_FOREGROUND)
    await session.commit()
    return {"created": created}


@router.post(
    "/genres/split-suggestions/{suggestion_id}/apply", response_model=ApplySuggestionResult
)
async def apply_split_suggestion(
    suggestion_id: int,
    body: ApplySuggestionBody,
    session: AsyncSession = Depends(get_session),
):
    """提案を適用する。子作成 / ルール移動 / 汎用降格 → 全件再分類（実測 47 秒）。

    提案が無い（LookupError）は 404、辞書が変わって提案が古くなっている
    （ValueError、子キーが別の親を持つジャンルと衝突）は 409。
    """
    from app.services.genre_split_store import apply_suggestion

    try:
        created, moved, reclassified = await apply_suggestion(
            session, suggestion_id, labels=body.labels
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return ApplySuggestionResult(created=created, moved=moved, reclassified=reclassified)


@router.post("/genres/split-suggestions/{suggestion_id}/dismiss", response_model=dict)
async def dismiss_split_suggestion(
    suggestion_id: int, session: AsyncSession = Depends(get_session)
):
    """その提案と同ジャンルの保留を閉じる。未読がこの時点より増えるまで再提案しない。"""
    from app.services.genre_split_store import dismiss_suggestion

    try:
        dismissed = await dismiss_suggestion(session, suggestion_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"dismissed": dismissed}


@router.patch("/genres/{genre_id}", response_model=GenreOut)
async def update_genre(
    genre_id: int, body: GenreUpdate, session: AsyncSession = Depends(get_session)
):
    genre = await session.get(Genre, genre_id)
    if not genre:
        raise HTTPException(status_code=404, detail="Genre not found")
    if body.label_ja is not None:
        genre.label_ja = body.label_ja.strip()
    if body.priority is not None:
        genre.priority = body.priority
    if "parent_id" in body.model_fields_set:
        await _validate_parent(session, body.parent_id, moving_id=genre_id)
        genre.parent_id = body.parent_id
    await session.commit()
    # priority を変えると解決順が変わるので再分類する
    changed = await _reclassify(session)
    out = next(g for g in await _list_genres(session) if g.id == genre_id)
    out.reclassified = changed
    return out


@router.delete("/genres/{genre_id}", response_model=ReclassifyResult)
async def delete_genre(genre_id: int, session: AsyncSession = Depends(get_session)):
    genre = await session.get(Genre, genre_id)
    if not genre:
        raise HTTPException(status_code=404, detail="Genre not found")
    await session.delete(genre)  # GenreRule は cascade で消える
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))


@router.post("/genre-rules", response_model=ReclassifyResult, status_code=201)
async def create_genre_rule(
    body: GenreRuleCreate, session: AsyncSession = Depends(get_session)
):
    tag = body.tag.strip().lower()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag must not be empty")
    if not await session.get(Genre, body.genre_id):
        raise HTTPException(status_code=404, detail="Genre not found")

    existing = (
        await session.execute(select(GenreRule).where(GenreRule.tag == tag))
    ).scalar_one_or_none()
    if existing:
        # 管理画面でタグを別ジャンルへ移す操作を自然にするため、衝突ではなく付け替え
        existing.genre_id = body.genre_id
        existing.is_generic = body.is_generic
    else:
        session.add(GenreRule(tag=tag, genre_id=body.genre_id, is_generic=body.is_generic))
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))


@router.delete("/genre-rules/{rule_id}", response_model=ReclassifyResult)
async def delete_genre_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    rule = await session.get(GenreRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await session.delete(rule)
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))


@router.post("/genres/seed-subgenres", response_model=SeedSubgenresResult)
async def seed_recommended_subgenres(session: AsyncSession = Depends(get_session)):
    """推奨サブジャンルを投入する。

    起動時の自動投入はしない。既存環境では数千件の genre 付け替えで FTS の
    再インデックスが走り（実測 6,408 件で約 15 秒）、押していない利用者から
    見れば「勝手に分類が変わった」になるため。
    """
    from app.services.genre_seed import seed_subgenres

    created, moved = await seed_subgenres(session)
    await session.commit()
    changed = await _reclassify(session) if (created or moved) else 0
    return SeedSubgenresResult(created=created, moved=moved, reclassified=changed)
