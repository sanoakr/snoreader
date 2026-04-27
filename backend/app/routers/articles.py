"""Article list, detail, state update, and search endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_session
from app.models import Article, ArticleTag, Feed
from app.schemas import (
    ArticleDetail,
    ArticleOut,
    ArticleUpdate,
    MarkAllReadRequest,
    PaginatedArticles,
)

router = APIRouter(tags=["articles"])


@router.get("/articles", response_model=PaginatedArticles)
async def list_articles(
    feed_id: int | None = None,
    is_read: bool | None = None,
    is_saved: bool | None = None,
    tag_id: int | None = None,
    sort: str = "published_at",
    order: str = "desc",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Article, Feed.title.label("feed_title")).join(Feed)
    count_stmt = select(func.count()).select_from(Article)

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)
        count_stmt = count_stmt.where(Article.feed_id == feed_id)
    if is_read is not None:
        stmt = stmt.where(Article.is_read == is_read)
        count_stmt = count_stmt.where(Article.is_read == is_read)
    if is_saved is not None:
        stmt = stmt.where(Article.is_saved == is_saved)
        count_stmt = count_stmt.where(Article.is_saved == is_saved)
    if tag_id is not None:
        stmt = stmt.where(Article.id.in_(select(ArticleTag.article_id).where(ArticleTag.tag_id == tag_id)))
        count_stmt = count_stmt.where(Article.id.in_(select(ArticleTag.article_id).where(ArticleTag.tag_id == tag_id)))

    # Sort
    allowed_sorts = {"published_at", "fetched_at", "title"}
    sort_col = getattr(Article, sort) if sort in allowed_sorts else Article.published_at
    stmt = stmt.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    stmt = stmt.offset(offset).limit(limit)

    total = (await session.execute(count_stmt)).scalar() or 0
    result = await session.execute(stmt)

    items = []
    for row in result:
        article = row[0]
        out = ArticleOut.model_validate(article)
        out.feed_title = row[1]
        items.append(out)

    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/articles/{article_id}", response_model=ArticleDetail)
async def get_article(article_id: int, session: AsyncSession = Depends(get_session)):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    if not article.content and article.url and not article.url.startswith("snoreader://"):
        from app.services.content_extractor import extract_content
        content = await extract_content(article.url)
        if content:
            article.content = content
            await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


@router.patch("/articles/{article_id}", response_model=ArticleOut)
async def update_article(
    article_id: int,
    body: ArticleUpdate,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    now = datetime.now(timezone.utc).isoformat()

    if body.is_read is not None:
        article.is_read = body.is_read
        article.read_at = now if body.is_read else None
    if body.is_saved is not None:
        article.is_saved = body.is_saved
        article.saved_at = now if body.is_saved else None

    await session.commit()
    await session.refresh(article)
    return ArticleOut.model_validate(article)


@router.post("/articles/mark-all-read", response_model=dict)
async def mark_all_read(
    body: MarkAllReadRequest,
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc).isoformat()
    stmt = select(Article).where(Article.is_read == False)  # noqa: E712
    if body.feed_id is not None:
        stmt = stmt.where(Article.feed_id == body.feed_id)

    result = await session.execute(stmt)
    articles = result.scalars().all()
    count = 0
    for article in articles:
        article.is_read = True
        article.read_at = now
        count += 1
    await session.commit()
    return {"marked": count}


@router.post("/articles/{article_id}/extract", response_model=ArticleDetail)
async def extract_article_content(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    from app.services.content_extractor import extract_content

    content = await extract_content(article.url)
    if content:
        article.content = content
        await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.one()
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


@router.post("/articles/{article_id}/summarize", response_model=ArticleDetail)
async def summarize_article(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Generate AI summary for an article."""
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    from app.ai.summarizer import summarize_article as _summarize

    text = article.content or article.summary or ""
    summary = await _summarize(article.title, text)
    if summary is None:
        raise HTTPException(status_code=503, detail="LLM server unavailable")

    article.ai_summary = summary
    await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.one()
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


@router.post("/articles/{article_id}/suggest-tags", response_model=list[str])
async def suggest_article_tags(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Suggest tags for an article using AI."""
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    from app.ai.tagger import suggest_tags

    text = article.content or article.summary or ""
    tags = await suggest_tags(article.title, text)
    if not tags:
        raise HTTPException(status_code=503, detail="LLM server unavailable or no tags generated")
    return tags


@router.get("/ai/status")
async def ai_status():
    """Check if the LLM server is available."""
    from app.ai.llm_client import is_available

    available = await is_available()
    return {"available": available, "base_url": settings.llm_base_url}


@router.get("/search", response_model=PaginatedArticles)
async def search_articles(
    q: str = Query(..., min_length=1),
    feed_id: int | None = None,
    is_saved: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    # FTS5 search
    fts_query = text(
        "SELECT rowid, rank FROM articles_fts WHERE articles_fts MATCH :q ORDER BY rank"
    )
    fts_result = await session.execute(fts_query, {"q": q})
    matching_ids = [row[0] for row in fts_result]

    if not matching_ids:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    stmt = select(Article, Feed.title.label("feed_title")).join(Feed).where(Article.id.in_(matching_ids))
    count_stmt = select(func.count()).select_from(Article).where(Article.id.in_(matching_ids))

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)
        count_stmt = count_stmt.where(Article.feed_id == feed_id)
    if is_saved is not None:
        stmt = stmt.where(Article.is_saved == is_saved)
        count_stmt = count_stmt.where(Article.is_saved == is_saved)

    total = (await session.execute(count_stmt)).scalar() or 0
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)

    items = []
    for row in result:
        article = row[0]
        out = ArticleOut.model_validate(article)
        out.feed_title = row[1]
        items.append(out)

    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/stats", response_model=list[dict])
async def get_stats(session: AsyncSession = Depends(get_session)):
    stmt = (
        select(
            Feed.id,
            Feed.title,
            func.count().label("total"),
            func.sum(case((Article.is_read == False, 1), else_=0)).label("unread"),  # noqa: E712
        )
        .join(Article, isouter=True)
        .group_by(Feed.id)
    )
    result = await session.execute(stmt)
    return [
        {"feed_id": row[0], "feed_title": row[1], "total": row[2] or 0, "unread": row[3] or 0}
        for row in result
    ]
