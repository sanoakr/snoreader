"""未保存・既読記事の保持期間経過後の自動削除機能のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    # Reload article_cleanup to ensure fresh reference to updated settings
    from app.services import article_cleanup as article_cleanup_module

    importlib.reload(article_cleanup_module)

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


async def _make_feed(session, url: str, title: str = ""):
    from app.models import Feed

    feed = Feed(url=url, title=title or None)
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, url: str, **kwargs):
    from app.models import Article

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=url,
        title=kwargs.pop("title", "Title"),
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_cleanup_deletes_old_unsaved_read_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_cleanup_keeps_saved_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=True, is_saved=True,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_unread_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=False, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_recent_unsaved_read_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/recent",
            published_at=_iso(89), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_falls_back_to_fetched_at_when_published_at_null(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=None, fetched_at=_iso(91), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_cleanup_respects_retention_days_override(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNOREADER_ARTICLE_RETENTION_DAYS を短く上書きすると、より新しい記事も削除対象になる。"""
    from app import config as config_module
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    monkeypatch.setattr(config_module.settings, "article_retention_days", 10)

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/mid",
            published_at=_iso(15), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []
