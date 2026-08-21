"""GET /api/tags/bulk-status の対象件数が各一括操作の実装と一致することのテスト。

この件数はタグ管理モーダルのボタンに「対象 N 件」として出るので、実際に処理される
件数とずれると「対象 0 件」と表示しながら動くボタンになる。定義のずれを防ぐために、
キーワード付与については実際に POST してから件数の変化も確認する。
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


async def _seed() -> dict[str, int]:
    """タグ数の異なる Saved 記事と、未読・未保存の記事を 1 件ずつ作る。"""
    from app.database import async_session
    from app.models import Article, ArticleTag, Feed, Tag

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        # name_ja あり 1 件、ASCII で未翻訳 2 件、非 ASCII で未翻訳 1 件（対象外）
        tags = [
            Tag(name="kubernetes", name_ja="クバネティス"),
            Tag(name="docker"),
            Tag(name="linux"),
            Tag(name="データ基盤"),
        ]
        for t in tags:
            session.add(t)
        await session.flush()

        def make(guid: str, title: str, *, is_saved: bool) -> Article:
            article = Article(
                feed_id=feed.id,
                guid=guid,
                url=f"https://example.com/{guid}",
                title=title,
                summary="body",
                is_saved=is_saved,
            )
            session.add(article)
            return article

        # タイトルに既存タグ名を入れておき、一括付与が実際にマッチするようにする
        untagged = make("untagged", "docker の入門", is_saved=True)
        two_tags = make("two", "linux の話", is_saved=True)
        four_tags = make("four", "linux のチューニング", is_saved=True)
        # 保存していない未タグ記事はどちらの対象にもならない
        not_saved = make("not-saved", "docker の別記事", is_saved=False)
        await session.flush()

        for tag in tags[:2]:
            session.add(ArticleTag(article_id=two_tags.id, tag_id=tag.id))
        for tag in tags:
            session.add(ArticleTag(article_id=four_tags.id, tag_id=tag.id))
        await session.commit()
        return {
            "untagged": untagged.id,
            "two": two_tags.id,
            "four": four_tags.id,
            "not_saved": not_saved.id,
        }


@pytest.mark.asyncio
async def test_counts_match_each_operation_definition(client: AsyncClient) -> None:
    await _seed()

    body = (await client.get("/api/tags/bulk-status")).json()

    # 未翻訳は ASCII 名のみ（'データ基盤' は数えない）
    assert body["untranslated_tags"] == 2
    # 0 タグ + 4 タグ以上。1〜3 タグの記事と未保存の記事は入らない
    assert body["keyword_targets"] == 2
    # AI 生成はタグ 0 件の Saved 記事だけ
    assert body["ai_targets"] == 1


@pytest.mark.asyncio
async def test_keyword_targets_shrink_after_running_the_operation(client: AsyncClient) -> None:
    """実際に一括付与を走らせたあと、件数が処理結果と整合していること。"""
    await _seed()
    before = (await client.get("/api/tags/bulk-status")).json()
    assert before["keyword_targets"] == 2

    resp = await client.post("/api/articles/auto-tag-saved")
    assert resp.status_code == 200

    after = (await client.get("/api/tags/bulk-status")).json()
    # 未タグ記事は docker が付いて 1 タグになり、4 タグ記事は剥がしてから linux が
    # 付き直して 1 タグになる。どちらも 1〜3 タグの帯に入るので対象から外れる
    assert after["keyword_targets"] == 0
    assert after["ai_targets"] == 0


@pytest.mark.asyncio
async def test_empty_database_reports_zeros(client: AsyncClient) -> None:
    body = (await client.get("/api/tags/bulk-status")).json()
    assert body == {"untranslated_tags": 0, "keyword_targets": 0, "ai_targets": 0}
