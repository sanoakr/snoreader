"""URL 除外パターン機能のテスト。

フェッチ時に URL がパターンにマッチした記事は保存されず、パターン新規追加時には
既存の未保存記事のうち一致するものもまとめて削除される。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.url_filters import is_excluded


# --- is_excluded のユニットテスト ---

def test_is_excluded_matches_substring():
    assert is_excluded("https://tonarinoyj.jp/episode/12207421983966650786", ["tonarinoyj.jp/episode/"])


def test_is_excluded_case_insensitive():
    assert is_excluded("https://Example.com/Episode/1", ["example.com/episode/"])


def test_is_excluded_no_match():
    assert not is_excluded("https://example.com/article/1", ["tonarinoyj.jp/episode/"])


def test_is_excluded_empty_patterns():
    assert not is_excluded("https://example.com/article/1", [])


# --- サービス層 / エンドポイントの統合テスト ---

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


async def _make_feed(session, url: str, title: str = ""):
    from app.models import Feed

    feed = Feed(url=url, title=title or None)
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, url: str, **kwargs):
    from app.models import Article
    from app.services.deduplicator import normalize_url

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=url,
        normalized_url=normalize_url(url),
        title=kwargs.pop("title", "Title"),
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_create_exclude_pattern(client: AsyncClient) -> None:
    res = await client.post("/api/exclude-patterns", json={"pattern": "tonarinoyj.jp/episode/"})
    assert res.status_code == 201
    data = res.json()
    assert data["pattern"] == "tonarinoyj.jp/episode/"
    assert data["purged"] == 0

    res2 = await client.get("/api/exclude-patterns")
    assert len(res2.json()) == 1


@pytest.mark.asyncio
async def test_create_exclude_pattern_rejects_duplicate(client: AsyncClient) -> None:
    await client.post("/api/exclude-patterns", json={"pattern": "tonarinoyj.jp/episode/"})
    res = await client.post("/api/exclude-patterns", json={"pattern": "tonarinoyj.jp/episode/"})
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_create_exclude_pattern_purges_existing_matches(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        feed = await _make_feed(session, "https://hatena.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://tonarinoyj.jp/episode/12207421983966650786"
        )
        await _make_article(session, feed.id, "g2", "https://example.com/normal-article")
        await session.commit()

    res = await client.post("/api/exclude-patterns", json={"pattern": "tonarinoyj.jp/episode/"})
    assert res.json()["purged"] == 1

    res2 = await client.get("/api/articles")
    assert res2.json()["total"] == 1


@pytest.mark.asyncio
async def test_create_exclude_pattern_never_purges_saved_articles(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        feed = await _make_feed(session, "https://hatena.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://tonarinoyj.jp/episode/12207421983966650786",
            is_saved=True,
        )
        await session.commit()

    res = await client.post("/api/exclude-patterns", json={"pattern": "tonarinoyj.jp/episode/"})
    assert res.json()["purged"] == 0

    res2 = await client.get("/api/articles")
    assert res2.json()["total"] == 1


@pytest.mark.asyncio
async def test_delete_exclude_pattern(client: AsyncClient) -> None:
    create_res = await client.post("/api/exclude-patterns", json={"pattern": "example.com/spam"})
    pattern_id = create_res.json()["id"]

    res = await client.delete(f"/api/exclude-patterns/{pattern_id}")
    assert res.status_code == 204

    res2 = await client.get("/api/exclude-patterns")
    assert res2.json() == []


@pytest.mark.asyncio
async def test_delete_exclude_pattern_not_found(client: AsyncClient) -> None:
    res = await client.delete("/api/exclude-patterns/9999")
    assert res.status_code == 404
