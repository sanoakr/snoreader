"""Article list, detail, state update, and search endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import Integer, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_session
from app.models import Article, ArticleTag, Feed, Tag
from app.schemas import (
    ArticleDetail,
    ArticleOut,
    ArticleUpdate,
    MarkAllReadRequest,
    PaginatedArticles,
    TagSuggestion,
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


@router.get("/articles/recommended", response_model=PaginatedArticles)
async def get_recommended_articles(
    sort: str = Query("score", pattern="^(score|date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return unread articles ranked by tag frequency overlap with saved articles."""
    import json as _json

    # Tag frequency map from saved articles: {tag_name: count}
    freq_stmt = (
        select(Tag.name, func.count(ArticleTag.article_id).label("freq"))
        .join(ArticleTag, Tag.id == ArticleTag.tag_id)
        .join(Article, ArticleTag.article_id == Article.id)
        .where(Article.is_saved == True)  # noqa: E712
        .group_by(Tag.name)
    )
    tag_freq: dict[str, int] = {
        row[0]: row[1] for row in (await session.execute(freq_stmt))
    }
    if not tag_freq:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    # Unread unsaved articles with pre-computed tag suggestions
    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .where(
            Article.is_read == False,  # noqa: E712
            Article.is_saved == False,  # noqa: E712
            Article.tag_suggestions.isnot(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    # Frequency-weighted score
    scored: list[tuple[int, Article, str]] = []
    for row in rows:
        article, feed_title = row[0], row[1]
        suggestions = set(_json.loads(article.tag_suggestions))
        score = sum(tag_freq.get(t, 0) for t in suggestions if t in tag_freq)
        if score > 0:
            scored.append((score, article, feed_title))

    def _pub_ts(a: Article) -> float:
        try:
            return datetime.fromisoformat(a.published_at).timestamp() if a.published_at else 0.0
        except (ValueError, TypeError):
            return 0.0

    if sort == "score":
        scored.sort(key=lambda x: (-x[0] if order == "desc" else x[0], -_pub_ts(x[1])))
    else:
        scored.sort(key=lambda x: (-_pub_ts(x[1]) if order == "desc" else _pub_ts(x[1])))

    total = len(scored)
    page = scored[offset: offset + limit]

    items = []
    for score, article, feed_title in page:
        out = ArticleOut.model_validate(article)
        out.feed_title = feed_title
        out.rec_score = score
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

        from app.ai.processor import summarize_and_tag
        from app.ai.task_queue import PRIORITY_FOREGROUND
        existing = await session.execute(select(Tag.name))
        existing_names = list(existing.scalars())
        text = content or article.summary or ""
        summary, pairs = await summarize_and_tag(
            article.title, text, existing_tags=existing_names, priority=PRIORITY_FOREGROUND
        )
        if summary:
            article.ai_summary = summary
            if pairs:
                import json as _json
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])

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
    from app.ai.task_queue import PRIORITY_FOREGROUND

    text = article.content or article.summary or ""
    summary = await _summarize(article.title, text, priority=PRIORITY_FOREGROUND)
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


@router.post("/articles/{article_id}/suggest-tags", response_model=list[TagSuggestion])
async def suggest_article_tags(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return pre-computed tag suggestions, or generate via AI if not yet available."""
    import json as _json

    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # Use pre-computed suggestions from summarize_and_tag when available
    if article.tag_suggestions:
        en_names = _json.loads(article.tag_suggestions)
        if en_names:
            tag_result = await session.execute(select(Tag).where(Tag.name.in_(en_names)))
            tag_map = {t.name: t.name_ja for t in tag_result.scalars()}
            return [TagSuggestion(name=en, name_ja=tag_map.get(en)) for en in en_names]

    # Fallback: generate via combined LLM call
    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_FOREGROUND

    existing = await session.execute(select(Tag.name))
    existing_names = list(existing.scalars())
    text = article.ai_summary or article.content or article.summary or ""
    _, pairs = await summarize_and_tag(
        article.title, text, existing_tags=existing_names, priority=PRIORITY_FOREGROUND
    )
    if not pairs:
        raise HTTPException(status_code=503, detail="LLM server unavailable or no tags generated")
    return [TagSuggestion(name=en, name_ja=ja) for en, ja in pairs]


_BULK_TAG_BATCH = 10  # max articles per batch


@router.post("/articles/ai-tag-saved", response_model=dict)
async def ai_tag_saved_articles(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Run AI tag suggestions on saved articles without tags and auto-assign them (max 10 per call)."""
    stmt = select(Article).where(Article.is_saved == True).options(selectinload(Article.tags))  # noqa: E712
    result = await session.execute(stmt)
    untagged_ids = [a.id for a in result.scalars() if not a.tags]
    batch = untagged_ids[:_BULK_TAG_BATCH]
    if batch:
        background_tasks.add_task(_bulk_tag_job, batch)
    return {"queued": len(batch), "remaining": len(untagged_ids) - len(batch)}


async def _bulk_tag_job(article_ids: list[int]) -> None:
    """Background task: batch-assign AI tags to saved articles."""
    from app.ai.tagger import suggest_tags
    from app.database import async_session

    async with async_session() as session:
        # Fetch existing tags once and share across all articles
        existing_result = await session.execute(select(Tag))
        existing_names = [t.name for t in existing_result.scalars()]

        for article_id in article_ids:
            article = await session.get(Article, article_id)
            if not article:
                continue
            text = article.ai_summary or article.content or article.summary or ""
            pairs = await suggest_tags(article.title, text, existing_tags=existing_names)
            for tag_name, tag_name_ja in pairs:
                result = await session.execute(select(Tag).where(Tag.name == tag_name))
                tag = result.scalar_one_or_none()
                if not tag:
                    tag = Tag(name=tag_name, name_ja=tag_name_ja)
                    session.add(tag)
                    await session.flush()
                elif tag_name_ja and not tag.name_ja:
                    tag.name_ja = tag_name_ja
                dup = await session.execute(
                    select(ArticleTag).where(
                        ArticleTag.article_id == article_id,
                        ArticleTag.tag_id == tag.id,
                    )
                )
                if not dup.scalar_one_or_none():
                    session.add(ArticleTag(article_id=article_id, tag_id=tag.id))
            await session.commit()


@router.get("/ai/status")
async def ai_status(session: AsyncSession = Depends(get_session)):
    """Check LLM availability and background processing queue depth."""
    from app.ai.llm_client import is_available
    from app.ai.task_queue import queue_depth
    from app.services.background_processor import is_running

    available = await is_available()
    pending_summary = await session.scalar(
        select(func.count()).select_from(Article).where(Article.ai_summary.is_(None))
    )
    pending_tags = await session.scalar(
        select(func.count()).select_from(Article).where(
            Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None)
        )
    )
    return {
        "available": available,
        "base_url": settings.llm_base_url,
        "running": is_running(),
        "queue_depth": queue_depth(),
        "pending_summary": pending_summary,
        "pending_tags": pending_tags,
    }


def _to_fts_query(raw: str) -> str:
    """Convert a user-typed string into a safe FTS5 phrase query.

    FTS5 はバッククオートや記号を演算子として解釈するため、利用者が入力した
    文字列は「フレーズ」として二重引用符で括り、内部の `"` のみエスケープする。
    空白で区切られた語は AND 検索になるよう、語ごとにフレーズ化する。
    """
    tokens = [t for t in raw.split() if t]
    if not tokens:
        return ""
    return " ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)


@router.get("/search", response_model=PaginatedArticles)
async def search_articles(
    q: str = Query(..., min_length=1),
    feed_id: int | None = None,
    is_saved: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    fts_q = _to_fts_query(q)
    if not fts_q:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    # FTS5 search
    fts_query = text(
        "SELECT rowid, rank FROM articles_fts WHERE articles_fts MATCH :q ORDER BY rank"
    )
    fts_result = await session.execute(fts_query, {"q": fts_q})
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
