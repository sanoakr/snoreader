"""フィード横断の重複記事削除機能のテスト。

異なるフィード（特にはてなブックマーク経由）から同じ記事が取り込まれた場合に、
正規化 URL が一致するものを重複とみなし、優先順位に従って 1 件だけ残す。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.deduplicator import normalize_url


# --- normalize_url のユニットテスト（DB 不要） ---

def test_normalize_url_strips_utm_params():
    a = normalize_url("https://example.com/article?utm_source=hatena&utm_medium=social")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_strips_known_tracking_params():
    a = normalize_url("https://example.com/article?fbclid=abc123")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_keeps_non_tracking_query():
    """?v= (YouTube) や ?p= (WordPress) のような識別子クエリは保持する。"""
    a = normalize_url("https://example.com/?p=123")
    b = normalize_url("https://example.com/?p=456")
    assert a != b


def test_normalize_url_strips_fragment():
    a = normalize_url("https://example.com/article#section2")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_strips_trailing_slash():
    a = normalize_url("https://example.com/article/")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_scheme_insensitive():
    a = normalize_url("http://example.com/article")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_host_lowercased():
    a = normalize_url("https://Example.COM/article")
    b = normalize_url("https://example.com/article")
    assert a == b


def test_normalize_url_query_order_insensitive():
    a = normalize_url("https://example.com/article?a=1&b=2")
    b = normalize_url("https://example.com/article?b=2&a=1")
    assert a == b


def test_normalize_url_invalid_input_passthrough():
    assert normalize_url("not a url") == "not a url"


def test_normalize_url_empty_passthrough():
    assert normalize_url("") == ""


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
async def test_dedup_removes_duplicate_across_feeds(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        await _make_article(session, feed_a.id, "g1", "https://news.example.com/story?utm_source=x")
        await _make_article(session, feed_b.id, "g2", "https://news.example.com/story")
        await session.commit()

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.status_code == 200
    data = res.json()
    assert data["duplicate_groups"] == 1
    assert data["deleted"] == 1
    assert data["dry_run"] is False

    res2 = await client.get("/api/articles")
    assert res2.json()["total"] == 1


@pytest.mark.asyncio
async def test_dedup_dry_run_does_not_delete(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        await _make_article(session, feed_a.id, "g1", "https://news.example.com/story")
        await _make_article(session, feed_b.id, "g2", "https://news.example.com/story")
        await session.commit()

    res = await client.post("/api/articles/dedup", json={"dry_run": True})
    data = res.json()
    assert data["deleted"] == 1
    assert data["dry_run"] is True

    res2 = await client.get("/api/articles")
    assert res2.json()["total"] == 2


@pytest.mark.asyncio
async def test_dedup_prefers_non_hatena_feed(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    async with async_session() as session:
        hatena_feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry/it.rss")
        normal_feed = await _make_feed(session, "https://normal.example.com/feed")
        await _make_article(session, hatena_feed.id, "g1", "https://news.example.com/story")
        kept = await _make_article(session, normal_feed.id, "g2", "https://news.example.com/story")
        await session.commit()
        kept_id = kept.id

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    async with async_session() as session:
        remaining = (await session.execute(select(Article.id))).scalars().all()
        assert remaining == [kept_id]


@pytest.mark.asyncio
async def test_dedup_prefers_saved_over_hatena_priority(client: AsyncClient) -> None:
    """saved はどんな由来より優先して残す。"""
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    async with async_session() as session:
        hatena_feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry/it.rss")
        normal_feed = await _make_feed(session, "https://normal.example.com/feed")
        kept = await _make_article(
            session, hatena_feed.id, "g1", "https://news.example.com/story",
            is_saved=True, saved_at="2026-01-01T00:00:00+00:00",
        )
        await _make_article(session, normal_feed.id, "g2", "https://news.example.com/story")
        await session.commit()
        kept_id = kept.id

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    async with async_session() as session:
        remaining = (await session.execute(select(Article.id))).scalars().all()
        assert remaining == [kept_id]


@pytest.mark.asyncio
async def test_dedup_prefers_older_when_tied(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        older = await _make_article(
            session, feed_a.id, "g1", "https://news.example.com/story",
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        await _make_article(
            session, feed_b.id, "g2", "https://news.example.com/story",
            fetched_at="2026-02-01T00:00:00+00:00",
        )
        await session.commit()
        older_id = older.id

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    async with async_session() as session:
        remaining = (await session.execute(select(Article.id))).scalars().all()
        assert remaining == [older_id]


@pytest.mark.asyncio
async def test_dedup_merges_state_and_tags(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article, ArticleTag, Tag
    from sqlalchemy import select

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        keeper = await _make_article(
            session, feed_a.id, "g1", "https://news.example.com/story",
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        loser = await _make_article(
            session, feed_b.id, "g2", "https://news.example.com/story",
            fetched_at="2026-02-01T00:00:00+00:00",
            is_read=True, read_at="2026-02-01T00:00:00+00:00",
        )
        shared_tag = Tag(name="shared")
        loser_only_tag = Tag(name="loser-only")
        session.add_all([shared_tag, loser_only_tag])
        await session.flush()
        session.add(ArticleTag(article_id=keeper.id, tag_id=shared_tag.id))
        session.add(ArticleTag(article_id=loser.id, tag_id=shared_tag.id))
        session.add(ArticleTag(article_id=loser.id, tag_id=loser_only_tag.id))
        await session.commit()
        keeper_id = keeper.id

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    async with async_session() as session:
        result = await session.execute(
            select(Article).where(Article.id == keeper_id)
        )
        keeper_after = result.scalar_one()
        assert keeper_after.is_read is True

        tag_names = (
            await session.execute(
                select(Tag.name)
                .join(ArticleTag, ArticleTag.tag_id == Tag.id)
                .where(ArticleTag.article_id == keeper_id)
            )
        ).scalars().all()
        assert set(tag_names) == {"shared", "loser-only"}


@pytest.mark.asyncio
async def test_dedup_deleted_article_not_searchable(client: AsyncClient) -> None:
    """削除後に FTS インデックスからも消えていること（トリガー回帰確認）。"""
    from app.database import async_session

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        await _make_article(
            session, feed_a.id, "g1", "https://news.example.com/story",
            title="ユニークなテスト記事タイトル",
        )
        await _make_article(
            session, feed_b.id, "g2", "https://news.example.com/story",
            title="ユニークなテスト記事タイトル",
        )
        await session.commit()

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    res2 = await client.get("/api/search", params={"q": "ユニークなテスト記事タイトル"})
    assert res2.json()["total"] == 1


@pytest.mark.asyncio
async def test_dedup_backfills_missing_normalized_url(client: AsyncClient) -> None:
    """normalized_url を明示的に設定していない既存風データも一括掃除で拾える。"""
    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        feed_a = await _make_feed(session, "https://a.example.com/feed")
        feed_b = await _make_feed(session, "https://b.example.com/feed")
        session.add(Article(feed_id=feed_a.id, guid="g1", url="https://news.example.com/story", normalized_url=None))
        session.add(Article(feed_id=feed_b.id, guid="g2", url="https://news.example.com/story", normalized_url=None))
        await session.commit()

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["duplicate_groups"] == 1
    assert res.json()["deleted"] == 1
