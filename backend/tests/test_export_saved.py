"""GET /api/export/saved-articles に参照用要約の欄が載ること（kioku が毎晩全件を読む）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

EXISTING_KEYS = {
    "id", "title", "url", "author", "published_at", "saved_at",
    "feed", "summary", "ai_summary", "tags",
}
REFERENCE_KEYS = {
    "reference_summary", "reference_summary_at", "reference_summary_version",
    "reference_basis", "genre",
}


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

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await database_module.engine.dispose()


async def _seed_article(**kwargs) -> int:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = (await session.execute(select(Feed))).scalars().first()
        if feed is None:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
        kwargs.setdefault("title", "題")
        kwargs.setdefault("is_saved", True)
        kwargs.setdefault("saved_at", "2026-11-01T00:00:00+00:00")
        article = Article(feed_id=feed.id, **kwargs)
        session.add(article)
        await session.commit()
        return article.id


@pytest.mark.asyncio
async def test_export_includes_reference_fields(client: AsyncClient) -> None:
    await _seed_article(
        guid="a1",
        url="https://example.com/1",
        genre="ai_misc",
        reference_summary="【要旨】x",
        reference_summary_at="2026-11-22T12:34:56+00:00",
        reference_summary_version="qwen3.8:27b/ref-v1",
        reference_basis="content",
    )
    data = (await client.get("/api/export/saved-articles")).json()
    assert len(data) == 1
    item = data[0]
    assert item["reference_summary"] == "【要旨】x"
    assert item["reference_summary_at"] == "2026-11-22T12:34:56+00:00"
    assert item["reference_summary_version"] == "qwen3.8:27b/ref-v1"
    assert item["reference_basis"] == "content"
    assert item["genre"] == "ai_misc"


@pytest.mark.asyncio
async def test_export_returns_null_reference_fields_when_missing(client: AsyncClient) -> None:
    await _seed_article(guid="a1", url="https://example.com/1", ai_summary="・要約")
    item = (await client.get("/api/export/saved-articles")).json()[0]
    assert REFERENCE_KEYS <= item.keys()  # キーは必ずある
    assert item["reference_summary"] is None
    assert item["reference_summary_at"] is None
    assert item["reference_summary_version"] is None
    assert item["reference_basis"] is None
    assert item["ai_summary"] == "・要約"  # kioku はこのとき ai_summary を代わりに使う


@pytest.mark.asyncio
async def test_export_keeps_existing_fields(client: AsyncClient) -> None:
    await _seed_article(guid="a1", url="https://example.com/1")
    item = (await client.get("/api/export/saved-articles")).json()[0]
    assert EXISTING_KEYS <= item.keys()


@pytest.mark.asyncio
async def test_export_excludes_unsaved(client: AsyncClient) -> None:
    await _seed_article(guid="a1", url="https://example.com/1", is_saved=False)
    assert (await client.get("/api/export/saved-articles")).json() == []
