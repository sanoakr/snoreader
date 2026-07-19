"""Tests for POST /api/articles/regenerate-summaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # config / database / main は環境変数を読み込んだあとに import する必要がある
    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    from app.database import async_session
    from app.models import Article, Feed

    async with main_module.lifespan(main_module.app):
        async with async_session() as session:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
            session.add_all(
                [
                    Article(
                        feed_id=feed.id,
                        guid="a1",
                        url="https://example.com/1",
                        title="要約済み記事",
                        summary="元の本文",
                        ai_summary="・既存の要約",
                        tag_suggestions='["existing"]',
                    ),
                    Article(
                        feed_id=feed.id,
                        guid="a2",
                        url="https://example.com/2",
                        title="未要約の記事",
                        summary="元の本文2",
                    ),
                ]
            )
            await session.commit()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_regenerate_summaries_clears_only_existing(client: AsyncClient) -> None:
    """ai_summary が設定済みの記事だけを NULL に戻し、件数を返すこと。"""
    res = await client.post("/api/articles/regenerate-summaries")
    assert res.status_code == 200
    assert res.json() == {"cleared": 1}

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        articles = (await session.execute(select(Article))).scalars().all()
        by_guid = {a.guid: a for a in articles}
        assert by_guid["a1"].ai_summary is None
        assert by_guid["a2"].ai_summary is None  # already None, unaffected


@pytest.mark.asyncio
async def test_regenerate_summaries_no_articles_returns_zero(client: AsyncClient) -> None:
    """ai_summary が1件も設定されていない場合は cleared: 0 を返すこと。"""
    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        await session.execute(update(Article).values(ai_summary=None))
        await session.commit()

    res = await client.post("/api/articles/regenerate-summaries")
    assert res.status_code == 200
    assert res.json() == {"cleared": 0}
