"""Tag CRUD and article-tag management endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Article, ArticleTag, Tag
from app.schemas import TagCreate, TagOut

router = APIRouter(tags=["tags"])


@router.get("/tags", response_model=list[TagOut])
async def list_tags(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Tag).order_by(Tag.name))
    return [TagOut.model_validate(t) for t in result.scalars()]


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(body: TagCreate, session: AsyncSession = Depends(get_session)):
    existing = await session.execute(select(Tag).where(Tag.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tag already exists")
    tag = Tag(name=body.name)
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: int, session: AsyncSession = Depends(get_session)):
    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await session.delete(tag)
    await session.commit()


@router.post("/articles/{article_id}/tags", response_model=TagOut)
async def add_tag_to_article(
    article_id: int,
    body: TagCreate,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    result = await session.execute(select(Tag).where(Tag.name == body.name))
    tag = result.scalar_one_or_none()
    if not tag:
        tag = Tag(name=body.name)
        session.add(tag)
        await session.flush()

    existing = await session.execute(
        select(ArticleTag).where(
            ArticleTag.article_id == article_id,
            ArticleTag.tag_id == tag.id,
        )
    )
    if not existing.scalar_one_or_none():
        session.add(ArticleTag(article_id=article_id, tag_id=tag.id))

    await session.commit()
    return TagOut.model_validate(tag)


@router.delete("/articles/{article_id}/tags/{tag_id}", status_code=204)
async def remove_tag_from_article(
    article_id: int,
    tag_id: int,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ArticleTag).where(
            ArticleTag.article_id == article_id,
            ArticleTag.tag_id == tag_id,
        )
    )
    at = result.scalar_one_or_none()
    if not at:
        raise HTTPException(status_code=404, detail="Tag not found on article")
    await session.delete(at)
    await session.commit()
