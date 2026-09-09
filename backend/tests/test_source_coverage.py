"""購読フィードが自前で配信しているホスト集合の導出のテスト。

引用フィード（はてなブックマーク）から、すでに購読しているサイトの記事を
取り込まないようにするための判定材料。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


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


async def _make_feed(session, url: str):
    from app.models import Feed

    feed = Feed(url=url)
    session.add(feed)
    await session.flush()
    return feed


async def _make_articles(session, feed_id: int, base_url: str, count: int, *, start: int = 0):
    from app.models import Article
    from app.services.deduplicator import normalize_url

    for i in range(start, start + count):
        url = f"{base_url}/{i}"
        session.add(
            Article(
                feed_id=feed_id,
                guid=f"{feed_id}-{url}",
                url=url,
                normalized_url=normalize_url(url),
                title=f"t{i}",
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_subscribed_hosts_collects_dominant_host(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, feed.id, "https://zenn.dev/u/articles", 10)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"zenn.dev"}


@pytest.mark.asyncio
async def test_subscribed_hosts_ignores_quote_feeds(client: AsyncClient) -> None:
    """はてブ自身が配信するホストは購読済み扱いにしない（全部消えてしまう）。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await _make_articles(session, feed.id, "https://togetter.com/li", 50)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == set()


@pytest.mark.asyncio
async def test_subscribed_hosts_requires_minimum_article_count(client: AsyncClient) -> None:
    """混入 1〜2 件のホストでそのサイト全体を巻き添えにしない。

    WirelessWire のような小さいフィード（実測 37 件）では 1% が 1 件を意味してしまい、
    割合だけでは下限にならないので件数下限を併記している。
    """
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://wirelesswire.jp/feed/")
        await _make_articles(session, feed.id, "https://wirelesswire.jp/a", 35)
        await _make_articles(session, feed.id, "https://note.com/x", 2)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"wirelesswire.jp"}


@pytest.mark.asyncio
async def test_subscribed_hosts_requires_minimum_share(client: AsyncClient) -> None:
    """件数下限は超えても、そのフィードの 1% 未満なら混入とみなす。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://gigazine.net/news/rss_2.0/")
        await _make_articles(session, feed.id, "https://gigazine.net/news", 1000)
        await _make_articles(session, feed.id, "https://note.com/x", 4)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"gigazine.net"}


@pytest.mark.asyncio
async def test_subscribed_hosts_keeps_subdomains_separate(client: AsyncClient) -> None:
    """itmedia.co.jp を購読していても nlab.itmedia.co.jp は別ホスト扱い。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        await _make_articles(session, feed.id, "https://www.itmedia.co.jp/news", 100)
        await _make_articles(session, feed.id, "https://monoist.itmedia.co.jp/mn", 10)
        await session.commit()

    async with async_session() as session:
        hosts = await subscribed_hosts(session)

    assert hosts == {"itmedia.co.jp", "monoist.itmedia.co.jp"}
    assert "nlab.itmedia.co.jp" not in hosts


_RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>quote</title>
{items}
</channel></rss>"""


def _rss(urls: list[str]) -> str:
    items = "\n".join(
        f"<item><title>t{i}</title><link>{u}</link><guid>{u}</guid></item>"
        for i, u in enumerate(urls)
    )
    return _RSS_TEMPLATE.format(items=items)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _patch_http(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    import httpx

    async def _get(self, url, *args, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse(body)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)


@pytest.mark.asyncio
async def test_fetch_skips_subscribed_hosts_in_quote_feed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article
    from app.services.feed_fetcher import fetch_feed

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        quote = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await session.commit()
        quote_id = quote.id

    _patch_http(
        monkeypatch,
        _rss(
            [
                "https://zenn.dev/other/articles/aaa",  # 購読済みホスト -> スキップ
                "https://togetter.com/li/1",  # 購読していない -> 取り込む
            ]
        ),
    )

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, quote_id)
        new_count = await fetch_feed(feed, session)

    assert new_count == 1

    async with async_session() as session:
        urls = (
            await session.execute(select(Article.url).where(Article.feed_id == quote_id))
        ).scalars().all()
    assert urls == ["https://togetter.com/li/1"]


@pytest.mark.asyncio
async def test_fetch_keeps_subscribed_hosts_in_non_quote_feed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """スキップは引用フィードだけの挙動。購読フィード自身は当然そのまま取り込む。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article
    from app.services.feed_fetcher import fetch_feed

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        await session.commit()
        zenn_id = zenn.id

    _patch_http(monkeypatch, _rss(["https://zenn.dev/new/articles/bbb"]))

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, zenn_id)
        assert await fetch_feed(feed, session) == 1

    async with async_session() as session:
        count = len(
            (
                await session.execute(
                    select(Article.id).where(Article.url == "https://zenn.dev/new/articles/bbb")
                )
            ).scalars().all()
        )
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_skip_can_be_disabled_by_setting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import config as config_module
    from app.database import async_session
    from app.services.feed_fetcher import fetch_feed

    monkeypatch.setattr(
        config_module.settings, "skip_subscribed_hosts_in_quote_feeds", False
    )

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        quote = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await session.commit()
        quote_id = quote.id

    _patch_http(monkeypatch, _rss(["https://zenn.dev/other/articles/aaa"]))

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, quote_id)
        assert await fetch_feed(feed, session) == 1


@pytest.mark.asyncio
async def test_subscribed_hosts_endpoint(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        await session.commit()

    res = await client.get("/api/feeds/subscribed-hosts")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["hosts"] == ["zenn.dev"]
