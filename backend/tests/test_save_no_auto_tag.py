"""保存操作では既存タグを自動付与しないことのテスト。

自動付与は「未タグの記事を保存した直後」= まさに手動でタグを付けようとしている
場面で必ず発動し、しかも PATCH の応答に tags が無いため次の再取得まで見えない。
結果として「手動でタグを入力したら勝手にタグが増えた」ように見えていた。
同じキーワードマッチ結果はリーダーの「Suggested:」チップに出るので、付けるかは
ユーザーが選ぶ。明示操作の一括付与エンドポイントは従来どおり付与する。
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


async def _seed(*, is_saved: bool = False) -> int:
    """既存タグ 'kubernetes' と、それがタイトルに含まれる未タグ記事を作る。"""
    from app.database import async_session
    from app.models import Article, Feed, Tag

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        session.add(Tag(name="kubernetes", name_ja="クバネティス"))
        await session.flush()

        article = Article(
            feed_id=feed.id,
            guid="a1",
            url="https://example.com/a1",
            title="kubernetes のアップグレード手順",
            summary="クラスタ更新の話",
            is_saved=is_saved,
        )
        session.add(article)
        await session.flush()
        await session.commit()
        return article.id


@pytest.mark.asyncio
async def test_saving_does_not_attach_existing_tags(client: AsyncClient) -> None:
    article_id = await _seed()

    resp = await client.patch(f"/api/articles/{article_id}", json={"is_saved": True})
    assert resp.status_code == 200
    assert resp.json()["is_saved"] is True

    detail = (await client.get(f"/api/articles/{article_id}")).json()
    assert detail["tags"] == []


@pytest.mark.asyncio
async def test_saving_still_clears_dismissed(client: AsyncClient) -> None:
    """自動付与を外しても、保存が非表示を解除する動線は残っていること。"""
    article_id = await _seed()
    await client.post("/api/articles/dismiss", json={"ids": [article_id]})
    assert (await client.get(f"/api/articles/{article_id}")).json()["dismissed_at"] is not None

    await client.patch(f"/api/articles/{article_id}", json={"is_saved": True})
    assert (await client.get(f"/api/articles/{article_id}")).json()["dismissed_at"] is None


@pytest.mark.asyncio
async def test_explicit_bulk_auto_tag_still_attaches(client: AsyncClient) -> None:
    """⚙ メニューの一括「Auto tag」は明示操作なので従来どおり付与する。"""
    article_id = await _seed(is_saved=True)

    resp = await client.post("/api/articles/auto-tag-saved")
    assert resp.status_code == 200
    assert resp.json()["attached"] == 1

    detail = (await client.get(f"/api/articles/{article_id}")).json()
    assert [t["name"] for t in detail["tags"]] == ["kubernetes"]


@pytest.mark.asyncio
async def test_manual_tag_add_attaches_only_that_tag(client: AsyncClient) -> None:
    """手動入力は入力したタグだけを付ける（マッチする既存タグを巻き込まない）。"""
    article_id = await _seed()
    await client.patch(f"/api/articles/{article_id}", json={"is_saved": True})

    resp = await client.post(f"/api/articles/{article_id}/tags", json={"name": "infra"})
    assert resp.status_code == 200

    detail = (await client.get(f"/api/articles/{article_id}")).json()
    assert [t["name"] for t in detail["tags"]] == ["infra"]
