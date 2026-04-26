"""Article import from Inoreader export and other sources."""

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Article, Feed

router = APIRouter(tags=["import"])

_IMPORTED_FEED_URL = "snoreader://imported"

_FEED_URL_PATTERNS = re.compile(
    r"(/feed|/rss|/atom|\.xml|\.rss|\.atom|/feeds?/)",
    re.IGNORECASE,
)


def _looks_like_feed_url(url: str) -> bool:
    """Heuristic: does this URL look like an RSS/Atom feed endpoint?"""
    return bool(_FEED_URL_PATTERNS.search(url))


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()[:1000]


def _parse_reader_item(item: dict) -> dict:
    """Parse a Google Reader / Inoreader export format item."""
    url = ""
    for alt in item.get("alternate", []):
        if "href" in alt:
            url = alt["href"]
            break
    if not url:
        for c in item.get("canonical", []):
            if "href" in c:
                url = c["href"]
                break

    origin = item.get("origin", {})
    stream_id = origin.get("streamId", "")
    feed_url = stream_id.removeprefix("feed/") if stream_id.startswith("feed/") else ""

    published = item.get("published")
    published_at = None
    if isinstance(published, (int, float)):
        published_at = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()

    summary_raw = ""
    if "summary" in item:
        summary_raw = item["summary"].get("content", "")
    elif "content" in item:
        summary_raw = item["content"].get("content", "")

    if feed_url and not _looks_like_feed_url(feed_url):
        feed_url = ""

    return {
        "url": url,
        "title": item.get("title", ""),
        "summary": _strip_html(summary_raw) if summary_raw else "",
        "author": item.get("author"),
        "published_at": published_at,
        "feed_url": feed_url,
        "feed_title": origin.get("title"),
    }


def _parse_simple_item(item: dict) -> dict:
    """Parse a simple {url, title, ...} item."""
    return {
        "url": item.get("url", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "author": item.get("author"),
        "published_at": item.get("published_at"),
        "feed_url": item.get("feed_url"),
        "feed_title": item.get("feed_title"),
    }


@router.post("/import/articles")
async def import_articles(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
):
    """Import saved articles from JSON.

    Supported formats:
    - Inoreader / Google Reader export ({"items": [...]})
    - Simple JSON array ([{"url": "...", "title": "...", ...}])
    """
    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if isinstance(data, dict) and "items" in data:
        raw_items = [_parse_reader_item(i) for i in data["items"]]
    elif isinstance(data, list):
        if data and ("alternate" in data[0] or "origin" in data[0]):
            raw_items = [_parse_reader_item(i) for i in data]
        else:
            raw_items = [_parse_simple_item(i) for i in data]
    else:
        raise HTTPException(status_code=400, detail="Unrecognized format")

    items = [i for i in raw_items if i["url"]]
    if not items:
        raise HTTPException(status_code=400, detail="No articles with valid URLs found")

    feed_cache: dict[str, Feed] = {}
    created = 0
    skipped = 0
    feeds_created = 0

    async def _get_or_create_feed(url: str, title: str) -> Feed:
        nonlocal feeds_created
        if url in feed_cache:
            return feed_cache[url]
        result = await session.execute(select(Feed).where(Feed.url == url))
        feed = result.scalar_one_or_none()
        if not feed:
            feed = Feed(url=url, title=title or None)
            session.add(feed)
            await session.flush()
            feeds_created += 1
        feed_cache[url] = feed
        return feed

    for item in items:
        if item["feed_url"]:
            feed = await _get_or_create_feed(item["feed_url"], item["feed_title"] or "")
        else:
            feed = await _get_or_create_feed(_IMPORTED_FEED_URL, "Imported")

        existing = await session.execute(
            select(Article).where(
                Article.feed_id == feed.id,
                Article.guid == item["url"],
            )
        )
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        article = Article(
            feed_id=feed.id,
            guid=item["url"],
            url=item["url"],
            title=item["title"],
            summary=item["summary"],
            author=item["author"],
            published_at=item["published_at"],
            is_read=True,
            read_at=now,
            is_saved=True,
            saved_at=now,
        )
        session.add(article)
        created += 1

    await session.commit()
    return {
        "articles_created": created,
        "articles_skipped": skipped,
        "feeds_created": feeds_created,
        "total": len(items),
    }
