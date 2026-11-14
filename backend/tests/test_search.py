"""FTS5 検索エンドポイントのテスト。

中央ペインの検索窓に日本語キーワードを入力したとき、対象記事を含めて返却される
ことを確認する。SQLite の既定 ``unicode61`` トークナイザは CJK の部分一致に
対応しないため、``trigram`` トークナイザへ切り替える修正を回帰確認する。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


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
                        title="サイボウズ kintone AI で業務アプリを作成",
                        summary="現場主体で業務アプリを構築できる",
                    ),
                    Article(
                        feed_id=feed.id,
                        guid="a2",
                        url="https://example.com/2",
                        title="Tailscale で社内ネットワークを構築",
                        summary="VPN メッシュの紹介",
                    ),
                    Article(
                        feed_id=feed.id,
                        guid="a3",
                        url="https://example.com/3",
                        title="Python 入門",
                        summary="Hello world",
                    ),
                ]
            )
            await session.commit()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    # 次のテストで新しい engine が作られる前に、この engine の aiosqlite
    # 接続・バックグラウンドスレッドを確実に破棄する（テスト分離のため）。
    await database_module.engine.dispose()


@pytest.mark.asyncio
async def test_search_japanese_substring(client: AsyncClient) -> None:
    """日本語の部分一致キーワードでヒットすること。"""
    res = await client.get("/api/search", params={"q": "アプリ"})
    assert res.status_code == 200
    data = res.json()
    titles = [item["title"] for item in data["items"]]
    assert any("kintone" in t for t in titles), data


@pytest.mark.asyncio
async def test_search_two_char_japanese(client: AsyncClient) -> None:
    """trigram は 3 文字未満をトークン化しないが、LIKE 経由で 2 文字キーワードもヒットさせる。"""
    res = await client.get("/api/search", params={"q": "業務"})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1, data


@pytest.mark.asyncio
async def test_search_multi_token_and(client: AsyncClient) -> None:
    """空白区切りの複数語は AND 検索になる。"""
    res = await client.get("/api/search", params={"q": "業務 アプリ"})
    assert res.status_code == 200
    assert res.json()["total"] >= 1
    res2 = await client.get("/api/search", params={"q": "業務 Tailscale"})
    assert res2.json()["total"] == 0


@pytest.mark.asyncio
async def test_search_english(client: AsyncClient) -> None:
    """英単語の検索が動作すること。"""
    res = await client.get("/api/search", params={"q": "Tailscale"})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1


@pytest.mark.asyncio
async def test_search_special_chars_no_500(client: AsyncClient) -> None:
    """FTS5 の演算子記号が混じっても 500 にならず、健全に空結果を返すこと。"""
    for query in ['"', "C++", "AND OR", "(test)"]:
        res = await client.get("/api/search", params={"q": query})
        assert res.status_code == 200, (query, res.text)


@pytest.mark.asyncio
async def test_search_empty_query_rejected(client: AsyncClient) -> None:
    res = await client.get("/api/search", params={"q": ""})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_search_results_newest_first(client: AsyncClient) -> None:
    """検索結果は新しい記事から順に並ぶ（published_at 降順、未設定は fetched_at で代替）。"""
    from datetime import datetime

    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed_id = (await session.execute(select(Feed.id))).scalars().first()
        # 挿入順は古い→新しい→中間、と意図的にずらす
        session.add_all(
            [
                Article(feed_id=feed_id, guid="s-old", url="https://example.com/s1",
                        title="並び順 古い", published_at=datetime(2026, 1, 1)),
                Article(feed_id=feed_id, guid="s-new", url="https://example.com/s2",
                        title="並び順 新しい", published_at=datetime(2026, 3, 1)),
                Article(feed_id=feed_id, guid="s-mid", url="https://example.com/s3",
                        title="並び順 中間", published_at=datetime(2026, 2, 1)),
            ]
        )
        await session.commit()

    res = await client.get("/api/search", params={"q": "並び順"})
    assert res.status_code == 200
    titles = [item["title"] for item in res.json()["items"]]
    assert titles == ["並び順 新しい", "並び順 中間", "並び順 古い"]
