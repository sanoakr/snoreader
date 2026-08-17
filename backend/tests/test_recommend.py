"""Recommend の絞り込み条件のテスト。

スコアの値域は保存記事の件数とともに広がる（IDF 項が log(n_saved/freq + 1)）ため、
「弱い一致を落とす」条件を絶対値の floor で表すと保存が貯まるにつれて効かなくなる。
一致タグの本数という構造的な条件で表す。
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


# 保存記事の総数。カバレッジ除外（30%）に引っかからない freq を作るには
# 保存記事がある程度必要なので、テストごとに埋め草で嵩上げする
_SAVED_TOTAL = 20


def _saved_with(*tag_sets: list[str]) -> list[list[str]]:
    """指定したタグ構成の保存記事に、固有タグだけを持つ埋め草を足して _SAVED_TOTAL 件にする。

    埋め草のタグは 1 件ずつ固有なので、他のタグの freq には影響しない。
    """
    filler = [[f"filler{i}"] for i in range(_SAVED_TOTAL - len(tag_sets))]
    return [*tag_sets, *filler]


async def _seed(saved_tag_sets: list[list[str]], unread: list[tuple[str, list[str]]]) -> None:
    """保存記事にタグを付け、未読記事に tag_suggestions を持たせる。

    saved_tag_sets: 保存記事 1 件ごとの手動タグ
    unread: (タイトル, tag_suggestions)
    """
    from app.database import async_session
    from app.models import Article, ArticleTag, Feed, Tag

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        await session.flush()

        tag_by_name: dict[str, Tag] = {}
        for i, tags in enumerate(saved_tag_sets):
            article = Article(
                feed_id=feed.id,
                guid=f"saved{i}",
                url=f"https://example.com/saved{i}",
                title=f"保存{i}",
                summary="",
                is_read=True,
                is_saved=True,
            )
            session.add(article)
            await session.flush()
            for name in tags:
                if name not in tag_by_name:
                    tag = Tag(name=name)
                    session.add(tag)
                    await session.flush()
                    tag_by_name[name] = tag
                session.add(ArticleTag(article_id=article.id, tag_id=tag_by_name[name].id))

        for i, (title, suggestions) in enumerate(unread):
            session.add(
                Article(
                    feed_id=feed.id,
                    guid=f"unread{i}",
                    url=f"https://example.com/unread{i}",
                    title=title,
                    summary="",
                    tag_suggestions=json.dumps(suggestions),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_single_tag_overlap_is_not_recommended(client: AsyncClient) -> None:
    """一致タグが 1 本だけの記事は推薦しない。

    絶対値の floor では止まらない（タグ 1 本でも保存件数次第で二桁のスコアが付く）。
    """
    await _seed(
        saved_tag_sets=_saved_with(["python"], ["rust"], ["python", "rust"]),
        unread=[("単一一致", ["python", "料理"])],
    )

    res = await client.get("/api/articles/recommended")
    assert res.status_code == 200
    assert res.json()["total"] == 0


@pytest.mark.asyncio
async def test_two_tag_overlap_is_recommended(client: AsyncClient) -> None:
    await _seed(
        saved_tag_sets=_saved_with(["python"], ["rust"], ["python", "rust"]),
        unread=[("二重一致", ["python", "rust"])],
    )

    res = await client.get("/api/articles/recommended")
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "二重一致"
    assert body["items"][0]["rec_score"] > 0


@pytest.mark.asyncio
async def test_no_overlap_is_not_recommended(client: AsyncClient) -> None:
    await _seed(
        saved_tag_sets=_saved_with(["python"], ["rust"]),
        unread=[("無関係", ["料理", "旅行"])],
    )

    res = await client.get("/api/articles/recommended")
    assert res.json()["total"] == 0


@pytest.mark.asyncio
async def test_tags_pruned_by_coverage_do_not_count_toward_the_minimum(
    client: AsyncClient,
) -> None:
    """カバレッジ超過で除外されたタグは一致本数に数えない。

    除外は「そのタグでは好みを判別できない」という判断なので、本数の水増しに
    使えてしまうと単一一致の記事が通ってしまう。
    """
    # 保存記事すべてに generic が付く（カバー率 100% > 30%）ので generic は除外される。
    # python は 4/20 = 20% で残る
    await _seed(
        saved_tag_sets=[
            *[["generic", "python"] for _ in range(4)],
            *[["generic"] for _ in range(_SAVED_TOTAL - 4)],
        ],
        unread=[("汎用タグで水増し", ["generic", "python"])],
    )

    res = await client.get("/api/articles/recommended")
    assert res.json()["total"] == 0


@pytest.mark.asyncio
async def test_coverage_cutoff_prunes_a_tag_just_over_the_threshold(
    client: AsyncClient,
) -> None:
    """閾値をわずかに超えるカバー率のタグが除外されること。

    閾値そのものを固定するテスト。0.3 のままだと 25% のタグは残り、
    common + python の 2 本一致として推薦されてしまう。
    """
    # common は 5/20 = 25%（閾値 0.2 超）で除外、python は 3/20 = 15% で残る
    await _seed(
        saved_tag_sets=[
            *[["common", "python"] for _ in range(3)],
            *[["common"] for _ in range(2)],
            *[[f"filler{i}"] for i in range(_SAVED_TOTAL - 5)],
        ],
        unread=[("汎用タグ 1 本 + 具体タグ 1 本", ["common", "python"])],
    )

    res = await client.get("/api/articles/recommended")
    assert res.json()["total"] == 0
