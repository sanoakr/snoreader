"""Fetch and parse RSS/Atom feeds."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed

logger = logging.getLogger(__name__)


def _parse_date(entry: dict) -> str | None:
    """Extract published date as ISO8601 string."""
    for key in ("published_parsed", "updated_parsed"):
        tp = entry.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return None


_MIN_IMAGE_DIM = 200  # 両辺がこれ未満のサムネイルは著者アイコン扱いでスキップ


def _is_icon_sized(mt: dict) -> bool:
    """幅・高さ両方が _MIN_IMAGE_DIM 未満なら True（情報がなければ False）。"""
    try:
        w, h = int(mt.get("width", 0)), int(mt.get("height", 0))
        return w > 0 and h > 0 and w < _MIN_IMAGE_DIM and h < _MIN_IMAGE_DIM
    except (ValueError, TypeError):
        return False


def _extract_image(entry: dict) -> str | None:
    """Extract thumbnail/image URL from feed entry."""
    # media:thumbnail
    for mt in entry.get("media_thumbnail", []):
        if _is_icon_sized(mt):
            continue
        if url := mt.get("url"):
            return url
    # media:content with image type
    for mc in entry.get("media_content", []):
        if "image" in mc.get("medium", mc.get("type", "")):
            if _is_icon_sized(mc):
                continue
            if url := mc.get("url"):
                return url
    # enclosure
    for enc in entry.get("enclosures", []):
        if "image" in enc.get("type", ""):
            if url := enc.get("href", enc.get("url")):
                return url
    return None


def _summary_text(entry: dict) -> str:
    """Get plain-text summary, stripping HTML."""
    raw = entry.get("summary", "")
    if not raw:
        content_list = entry.get("content", [])
        if content_list:
            raw = content_list[0].get("value", "")
    # Simple HTML tag stripping
    import re
    text = re.sub(r"<[^>]+>", "", raw)
    return text.strip()[:1000]


async def fetch_feed(feed: Feed, session: AsyncSession) -> int:
    """Fetch a single feed and insert new articles. Returns count of new articles."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(feed.url)
            resp.raise_for_status()
    except Exception as e:
        feed.error_count += 1
        feed.last_error = str(e)
        await session.commit()
        logger.warning("Failed to fetch %s: %s", feed.url, e)
        return 0

    parsed = feedparser.parse(resp.text)

    # Update feed metadata on first fetch or if changed
    if parsed.feed.get("title") and not feed.title:
        feed.title = parsed.feed.get("title")
    if parsed.feed.get("link"):
        feed.site_url = parsed.feed.get("link")
    if parsed.feed.get("subtitle"):
        feed.description = parsed.feed.get("subtitle")
    if not feed.favicon_url and feed.site_url:
        from urllib.parse import urlparse
        origin = urlparse(feed.site_url)
        feed.favicon_url = f"{origin.scheme}://{origin.netloc}/favicon.ico"

    new_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for entry in parsed.entries:
        guid = entry.get("id", entry.get("link", ""))
        url = entry.get("link", "")
        if not guid or not url:
            continue

        stmt = sqlite_upsert(Article).values(
            feed_id=feed.id,
            guid=guid,
            url=url,
            title=entry.get("title", ""),
            summary=_summary_text(entry),
            author=entry.get("author"),
            image_url=_extract_image(entry),
            published_at=_parse_date(entry),
            fetched_at=now,
        ).on_conflict_do_nothing(index_elements=["feed_id", "guid"])

        result = await session.execute(stmt)
        if result.rowcount > 0:
            new_count += 1

    feed.last_fetched_at = now
    feed.error_count = 0
    feed.last_error = None
    await session.commit()

    logger.info("Fetched %s: %d new articles", feed.url, new_count)
    return new_count


async def fetch_all_feeds() -> None:
    """Fetch all feeds with parallel HTTP requests (max 5 concurrent)."""
    import asyncio

    from app.database import async_session

    async with async_session() as session:
        result = await session.execute(select(Feed))
        feed_ids = [f.id for f in result.scalars().all()]

    sem = asyncio.Semaphore(5)

    async def _fetch_one(feed_id: int) -> None:
        async with sem:
            async with async_session() as sess:
                feed = await sess.get(Feed, feed_id)
                if feed:
                    await fetch_feed(feed, sess)

    await asyncio.gather(*[_fetch_one(fid) for fid in feed_ids])
