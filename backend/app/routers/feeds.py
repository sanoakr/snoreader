"""Feed CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Article, Feed
from app.schemas import FeedCreate, FeedOut, FeedUpdate
from app.services.feed_fetcher import fetch_feed

router = APIRouter(tags=["feeds"])


@router.get("/feeds", response_model=list[FeedOut])
async def list_feeds(session: AsyncSession = Depends(get_session)):
    # Subquery for unread count
    unread_sub = (
        select(Article.feed_id, func.count().label("unread_count"))
        .where(Article.is_read == False)  # noqa: E712
        .group_by(Article.feed_id)
        .subquery()
    )
    stmt = select(Feed, func.coalesce(unread_sub.c.unread_count, 0).label("unread_count")).outerjoin(
        unread_sub, Feed.id == unread_sub.c.feed_id
    ).order_by(Feed.title)

    result = await session.execute(stmt)
    feeds = []
    for row in result:
        feed = row[0]
        feed_out = FeedOut.model_validate(feed)
        feed_out.unread_count = row[1]
        feeds.append(feed_out)
    return feeds


@router.post("/feeds", response_model=FeedOut, status_code=201)
async def create_feed(body: FeedCreate, session: AsyncSession = Depends(get_session)):
    url_str = str(body.url)
    existing = await session.execute(select(Feed).where(Feed.url == url_str))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Feed already exists")

    feed = Feed(url=url_str)
    session.add(feed)
    await session.commit()
    await session.refresh(feed)

    # Fetch articles immediately
    await fetch_feed(feed, session)
    await session.refresh(feed)

    return FeedOut.model_validate(feed)


@router.put("/feeds/{feed_id}", response_model=FeedOut)
async def update_feed(feed_id: int, body: FeedUpdate, session: AsyncSession = Depends(get_session)):
    feed = await session.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")

    if body.title is not None:
        feed.title = body.title
    if body.fetch_interval_minutes is not None:
        feed.fetch_interval_minutes = body.fetch_interval_minutes
    await session.commit()
    await session.refresh(feed)
    return FeedOut.model_validate(feed)


@router.delete("/feeds/{feed_id}", status_code=204)
async def delete_feed(feed_id: int, session: AsyncSession = Depends(get_session)):
    feed = await session.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    await session.delete(feed)
    await session.commit()


@router.post("/feeds/{feed_id}/refresh", response_model=dict)
async def refresh_feed(feed_id: int, session: AsyncSession = Depends(get_session)):
    feed = await session.get(Feed, feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    new_count = await fetch_feed(feed, session)
    return {"new_articles": new_count}
