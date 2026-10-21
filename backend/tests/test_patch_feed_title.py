"""PATCH /articles/{id} の応答に feed_title が入ることのテスト。

フロントは応答を既存キャッシュへ {...old, ...result} でマージするので、
null が返るとリーダーのタイトル下のフィード名が既読化・保存のたびに消える。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

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


async def _seed() -> int:
    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        await session.flush()
        article = Article(
            feed_id=feed.id,
            guid="a1",
            url="https://example.com/a1",
            title="title",
            summary="summary",
        )
        session.add(article)
        await session.flush()
        await session.commit()
        return article.id


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"is_read": True}, {"is_saved": True}])
async def test_patch_response_keeps_feed_title(client: AsyncClient, data: dict) -> None:
    article_id = await _seed()

    resp = await client.patch(f"/api/articles/{article_id}", json=data)

    assert resp.status_code == 200
    assert resp.json()["feed_title"] == "Test Feed"
