"""Export endpoints for saved articles."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.models import Article

router = APIRouter(tags=["export"])


@router.get("/export/saved-articles")
async def export_saved_articles(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Article)
        .where(Article.is_saved.is_(True))
        .options(selectinload(Article.feed), selectinload(Article.tags))
        .order_by(Article.saved_at.desc())
    )
    articles = result.scalars().all()

    data = [
        {
            "id": a.id,
            "title": a.title,
            "url": a.url,
            "author": a.author,
            "published_at": a.published_at,
            "saved_at": a.saved_at,
            "feed": {"id": a.feed.id, "title": a.feed.title, "url": a.feed.url} if a.feed else None,
            "summary": a.summary,
            "ai_summary": a.ai_summary,
            "tags": [{"name": t.name, "name_ja": t.name_ja} for t in a.tags],
        }
        for a in articles
    ]

    filename = f"snoreader-saved-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2).encode(),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
