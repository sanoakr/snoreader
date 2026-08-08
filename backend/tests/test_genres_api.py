"""ジャンル定義の CRUD と再分類のテスト。"""

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


@pytest.mark.asyncio
async def test_seed_creates_initial_genres(client: AsyncClient) -> None:
    """起動時のシードで初期ジャンルとルールが入ること。API はまだ無いので DB を直接見る。"""
    from app.database import async_session
    from app.models import Genre, GenreRule
    from sqlalchemy import select

    assert client is not None  # lifespan を通すためにフィクスチャを使う

    async with async_session() as session:
        genres = (await session.execute(select(Genre).order_by(Genre.priority))).scalars().all()
        keys = [g.key for g in genres]
        assert keys[0] == "ai"
        assert "dev" in keys
        assert "other" not in keys  # 予約キーは DB に持たない
        assert all(g.label_ja for g in genres)

        ai_id = next(g.id for g in genres if g.key == "ai")
        ai_tags = (
            await session.execute(select(GenreRule.tag).where(GenreRule.genre_id == ai_id))
        ).scalars().all()
        assert "llm" in ai_tags

        generic = (
            await session.execute(
                select(GenreRule.tag).where(GenreRule.is_generic == True)  # noqa: E712
            )
        ).scalars().all()
        assert generic == ["technology"]


@pytest.mark.asyncio
async def test_seed_runs_only_once(client: AsyncClient) -> None:
    """2 回目の呼び出しでシードが重複投入されないこと。"""
    from app.database import async_session
    from app.models import Genre
    from app.services.genre_seed import seed_genres
    from sqlalchemy import func, select

    assert client is not None

    async with async_session() as session:
        before = await session.scalar(select(func.count()).select_from(Genre))
        assert await seed_genres(session) == 0
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Genre)) == before


async def _make_feed(session, url: str = "https://example.com/feed"):
    from app.models import Feed

    feed = Feed(url=url, title="Test Feed")
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, tags: list[str] | None, **kwargs):
    import json

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


@pytest.mark.asyncio
async def test_genre_counts_group_unread_unsaved_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await _make_article(session, feed.id, "g2", ["ai", "programming"])
        await _make_article(session, feed.id, "g3", ["baseball"])
        await _make_article(session, feed.id, "g4", ["llm"], is_read=True)   # 既読は数えない
        await _make_article(session, feed.id, "g5", ["llm"], is_saved=True)  # 保存済みも数えない
        await _make_article(session, feed.id, "g6", None)                    # 未分類は出さない
        await reclassify_all(session)
        await session.commit()

    res = await client.get("/api/articles/genres")
    assert res.status_code == 200
    counts = {row["genre"]: row["unread_count"] for row in res.json()}
    assert counts["ai"] == 2
    assert counts["sports"] == 1
    assert "other" not in counts

    labels = {row["genre"]: row["label_ja"] for row in res.json()}
    assert labels["ai"] == "AI・LLM"


@pytest.mark.asyncio
async def test_genre_counts_label_for_reserved_other(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["working-holiday"])
        await reclassify_all(session)
        await session.commit()

    rows = res_json = (await client.get("/api/articles/genres")).json()
    other = next(r for r in rows if r["genre"] == "other")
    assert other["label_ja"] == "その他"
    assert res_json


@pytest.mark.asyncio
async def test_reclassify_all_returns_updated_count(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.genre_classifier import reclassify_all
    from sqlalchemy import select

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await _make_article(session, feed.id, "g2", ["baseball"])
        changed = await reclassify_all(session)
        await session.commit()
        assert changed == 2

        genres = sorted((await session.execute(select(Article.genre))).scalars().all())
        assert genres == ["ai", "sports"]

    async with async_session() as session:
        # 変化が無ければ 0 件（毎回 UPDATE を投げない）
        assert await reclassify_all(session) == 0
