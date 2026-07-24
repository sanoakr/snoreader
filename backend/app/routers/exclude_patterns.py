"""Exclude pattern CRUD — URL patterns skipped at feed-fetch time (see feed_fetcher.py)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Article, ExcludePattern
from app.schemas import ExcludePatternCreate, ExcludePatternOut

router = APIRouter(tags=["exclude-patterns"])


@router.get("/exclude-patterns", response_model=list[ExcludePatternOut])
async def list_exclude_patterns(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ExcludePattern).order_by(ExcludePattern.created_at))
    return result.scalars().all()


@router.post("/exclude-patterns", response_model=ExcludePatternOut, status_code=201)
async def create_exclude_pattern(body: ExcludePatternCreate, session: AsyncSession = Depends(get_session)):
    from app.services.url_filters import is_excluded

    pattern = body.pattern.strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="Pattern must not be empty")

    existing = await session.execute(select(ExcludePattern).where(ExcludePattern.pattern == pattern))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Pattern already exists")

    obj = ExcludePattern(pattern=pattern)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)

    # 既存記事のうち保存されていないものはこの場でまとめて削除する
    # （保存済みは article_cleanup.py と同じ方針で対象外）
    candidates = (
        await session.execute(
            select(Article.id, Article.url).where(Article.is_saved == False)  # noqa: E712
        )
    ).all()
    matched_ids = [aid for aid, url in candidates if is_excluded(url, [pattern])]
    purged = 0
    if matched_ids:
        result = await session.execute(
            Article.__table__.delete().where(Article.id.in_(matched_ids))
        )
        purged = result.rowcount or 0
        await session.commit()

    out = ExcludePatternOut.model_validate(obj)
    out.purged = purged
    return out


@router.delete("/exclude-patterns/{pattern_id}", status_code=204)
async def delete_exclude_pattern(pattern_id: int, session: AsyncSession = Depends(get_session)):
    obj = await session.get(ExcludePattern, pattern_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Pattern not found")
    await session.delete(obj)
    await session.commit()
