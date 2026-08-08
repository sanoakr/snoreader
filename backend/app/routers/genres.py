"""ジャンル定義の CRUD。

粒度と分け方は運用しながら変わるので、辞書はコード定数ではなく DB に置く。
変更のたびにその場で既存記事を再分類する（POST /exclude-patterns が追加時に
既存記事を purge するのと同じ作法）。LLM を呼ばないので数千件でも一瞬。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Genre, GenreRule
from app.schemas import (
    GenreCreate,
    GenreOut,
    GenreRuleCreate,
    GenreRuleOut,
    GenreUpdate,
    ReclassifyResult,
)

router = APIRouter(tags=["genres"])

# genres テーブルに行を持たない予約キー（どのルールにも当たらない記事の受け皿）
_RESERVED_KEYS = {"other"}


async def _reclassify(session: AsyncSession) -> int:
    from app.services.genre_classifier import reclassify_all

    changed = await reclassify_all(session)
    await session.commit()
    return changed


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

    genre = Genre(key=key, label_ja=body.label_ja.strip(), priority=body.priority)
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
        reclassified=changed,
    )


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
