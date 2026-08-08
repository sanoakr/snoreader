"""記事の非表示（dismissed）機能のテスト。

非表示は is_read を立てないため、article_cleanup の自動削除対象にならない。
"""

from __future__ import annotations

import importlib
import json
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


async def _make_feed(session, url: str = "https://example.com/feed"):
    from app.models import Feed

    feed = Feed(url=url, title="Test Feed")
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, tags: list[str] | None, **kwargs):
    from app.models import Article

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=f"https://example.com/{guid}",
        title=kwargs.pop("title", "Title"),
        tag_suggestions=json.dumps(tags) if tags is not None else None,
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


async def _seed_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["baseball"], title="野球1")
        await _make_article(session, feed.id, "g2", ["soccer"], title="サッカー1")
        await _make_article(session, feed.id, "g3", ["llm"], title="AI1")
        await _make_article(session, feed.id, "g4", ["baseball"], title="保存野球", is_saved=True)
        await reclassify_all(session)
        await session.commit()


@pytest.mark.asyncio
async def test_dismiss_by_genre_hides_only_that_genre(client: AsyncClient) -> None:
    await _seed_articles(client)

    res = await client.post("/api/articles/dismiss", json={"genre": "sports"})
    assert res.status_code == 200
    assert res.json()["dismissed"] == 2  # 保存済みは対象外

    listed = (await client.get("/api/articles")).json()
    titles = {item["title"] for item in listed["items"]}
    assert titles == {"AI1", "保存野球"}


@pytest.mark.asyncio
async def test_dismiss_protects_saved_articles_by_ids(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    await _seed_articles(client)
    async with async_session() as session:
        saved_id = (
            await session.execute(select(Article.id).where(Article.is_saved == True))  # noqa: E712
        ).scalars().first()

    res = await client.post("/api/articles/dismiss", json={"ids": [saved_id]})
    assert res.json()["dismissed"] == 0


@pytest.mark.asyncio
async def test_dismissed_articles_excluded_from_lists(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert "sports" not in counts

    feeds = (await client.get("/api/feeds")).json()
    # フィードの unread_count は is_read のみに基づく既存仕様で is_saved を見ないため、
    # AI1（未読・未保存）と保存野球（未読・保存済み）の 2 件が対象になる。
    # dismiss された sports の 2 件（野球1・サッカー1）だけが減る。
    assert feeds[0]["unread_count"] == 2

    unrec = (await client.get("/api/articles/unrecommended")).json()
    assert all("野球" not in item["title"] for item in unrec["items"])


@pytest.mark.asyncio
async def test_dismissed_articles_visible_in_search(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.get("/api/search", params={"q": "野球"})
    titles = {item["title"] for item in res.json()["items"]}
    assert "野球1" in titles
    assert next(i for i in res.json()["items"] if i["title"] == "野球1")["dismissed_at"] is not None


@pytest.mark.asyncio
async def test_undismiss_restores_articles(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.post("/api/articles/undismiss", json={"genre": "sports"})
    assert res.json()["restored"] == 2

    listed = (await client.get("/api/articles")).json()
    assert listed["total"] == 4


@pytest.mark.asyncio
async def test_dismissed_view_lists_only_dismissed(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.get("/api/articles", params={"dismissed": "true"})
    titles = {item["title"] for item in res.json()["items"]}
    assert titles == {"野球1", "サッカー1"}


@pytest.mark.asyncio
async def test_dismiss_requires_genre_or_ids(client: AsyncClient) -> None:
    res = await client.post("/api/articles/dismiss", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_mark_all_read_by_genre_protects_saved(client: AsyncClient) -> None:
    await _seed_articles(client)

    res = await client.post("/api/articles/mark-all-read", json={"genre": "sports"})
    assert res.json()["marked"] == 2  # 保存済みの「保存野球」は既読にしない

    saved = (await client.get("/api/articles", params={"is_saved": "true"})).json()
    assert saved["items"][0]["is_read"] is False
