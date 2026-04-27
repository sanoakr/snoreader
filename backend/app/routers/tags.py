"""Tag CRUD and article-tag management endpoints."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Article, ArticleTag, Tag
from app.schemas import BulkDeleteTagsRequest, TagCreate, TagOut, TagSuggestion, TagUpdate

router = APIRouter(tags=["tags"])


@router.post("/tags/fill-translations", response_model=dict)
async def fill_tag_translations(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """name_ja が未設定の英語タグを LLM で一括翻訳する。"""
    result = await session.execute(select(Tag).where(Tag.name_ja.is_(None)))
    missing = [t for t in result.scalars() if t.name.isascii()]
    if not missing:
        return {"translated": 0}
    names = [t.name for t in missing]
    background_tasks.add_task(_fill_translations_job, names)
    return {"translating": len(names)}


async def _fill_translations_job(names: list[str]) -> None:
    from app.ai.tagger import translate_tags
    from app.database import async_session

    mapping = await translate_tags(names)
    if not mapping:
        return
    async with async_session() as session:
        for name, ja in mapping.items():
            result = await session.execute(select(Tag).where(Tag.name == name))
            tag = result.scalar_one_or_none()
            if tag:
                tag.name_ja = ja
        await session.commit()


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


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def rename_tag(tag_id: int, body: TagUpdate, session: AsyncSession = Depends(get_session)):
    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    dup = await session.execute(select(Tag).where(Tag.name == body.name, Tag.id != tag_id))
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tag name already exists")
    tag.name = body.name
    await session.commit()
    await session.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/tags/bulk", response_model=dict)
async def bulk_delete_tags(body: BulkDeleteTagsRequest, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Tag).where(Tag.id.in_(body.tag_ids)))
    tags_to_delete = result.scalars().all()
    for tag in tags_to_delete:
        await session.delete(tag)
    await session.commit()
    return {"deleted": len(tags_to_delete)}


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
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    input_name = body.name.strip()

    if input_name.isascii():
        # 英語入力: name に格納、name_ja は後でバックグラウンド補完
        en_name = input_name.lower()
        ja_name = body.name_ja
    else:
        # 日本語入力: LLM で英語名を生成して name に格納
        from app.ai.tagger import translate_to_english
        ja_name = input_name
        en_name = await translate_to_english(input_name)
        if not en_name:
            raise HTTPException(status_code=503, detail="LLM unavailable — cannot translate Japanese tag to English. Please enter an English tag name.")

    result = await session.execute(select(Tag).where(Tag.name == en_name))
    tag = result.scalar_one_or_none()
    if not tag:
        tag = Tag(name=en_name, name_ja=ja_name)
        session.add(tag)
        await session.flush()
    elif ja_name and not tag.name_ja:
        tag.name_ja = ja_name

    existing = await session.execute(
        select(ArticleTag).where(
            ArticleTag.article_id == article_id,
            ArticleTag.tag_id == tag.id,
        )
    )
    if not existing.scalar_one_or_none():
        session.add(ArticleTag(article_id=article_id, tag_id=tag.id))

    await session.commit()

    # 英語タグで name_ja 未設定の場合はバックグラウンドで翻訳
    if tag.name_ja is None:
        background_tasks.add_task(_translate_single_tag, tag.id)

    return TagOut.model_validate(tag)


async def _translate_single_tag(tag_id: int) -> None:
    """新規英語タグの name_ja をバックグラウンドで LLM 翻訳する。"""
    from app.ai.tagger import translate_tags
    from app.database import async_session

    async with async_session() as session:
        tag = await session.get(Tag, tag_id)
        if not tag or tag.name_ja:
            return
        mapping = await translate_tags([tag.name])
        if tag.name in mapping:
            tag.name_ja = mapping[tag.name]
            await session.commit()


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
