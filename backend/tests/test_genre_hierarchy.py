"""ジャンルの親子階層のテスト。

階層は 2 段固定で、Article.genre は葉のキーを持つ。親の件数は子の合計として
集計時に導出するので、記事側のスキーマ変更は無い。
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


@pytest.mark.asyncio
async def test_genres_table_has_parent_id_column(client: AsyncClient) -> None:
    """create_all は既存テーブルを変更しないので、手動 ALTER TABLE が必要。"""
    from sqlalchemy import text

    from app.database import engine

    async with engine.connect() as conn:
        rows = (await conn.execute(text("PRAGMA table_info(genres)"))).fetchall()
    assert "parent_id" in {row[1] for row in rows}


@pytest.mark.asyncio
async def test_child_genre_links_to_parent(client: AsyncClient) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import async_session
    from app.models import Genre

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        session.add(Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id))
        await session.commit()

    async with async_session() as session:
        parent = (
            await session.execute(
                select(Genre).options(selectinload(Genre.children)).where(Genre.key == "ai")
            )
        ).scalar_one()
        assert [c.key for c in parent.children] == ["ai_llm"]


@pytest.mark.asyncio
async def test_deleting_parent_deletes_children(client: AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import Genre

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        session.add(Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id))
        await session.commit()

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        await session.delete(parent)
        await session.commit()
        remaining = await session.scalar(
            select(func.count()).select_from(Genre).where(Genre.key == "ai_llm")
        )
        assert remaining == 0


async def _seed_hierarchy() -> None:
    """ai の下に ai_llm を作り、llm タグを子に付け替える。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Genre, GenreRule

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        child = Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id)
        session.add(child)
        await session.flush()
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == "llm"))
        ).scalar_one()
        rule.genre_id = child.id
        await session.commit()


async def _make_article(guid: str, genre: str, **kwargs) -> int:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = (await session.execute(select(Feed))).scalars().first()
        if feed is None:
            feed = Feed(url="https://example.com/feed", title="Test Feed")
            session.add(feed)
            await session.flush()
        article = Article(
            feed_id=feed.id,
            guid=guid,
            url=f"https://example.com/{guid}",
            title=kwargs.pop("title", "Title"),
            summary="",
            genre=genre,
            **kwargs,
        )
        session.add(article)
        await session.flush()
        await session.commit()
        return article.id


@pytest.mark.asyncio
async def test_genre_keys_expands_to_descendants(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_scope import genre_keys

    await _seed_hierarchy()
    async with async_session() as session:
        assert sorted(await genre_keys(session, "ai")) == ["ai", "ai_llm"]
        assert await genre_keys(session, "ai_llm") == ["ai_llm"]
        assert await genre_keys(session, "ai", exact=True) == ["ai"]
        # genres に行を持たない予約キーはそのまま返す
        assert await genre_keys(session, "other") == ["other"]


@pytest.mark.asyncio
async def test_list_articles_by_parent_includes_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai")
    assert resp.status_code == 200
    assert {a["guid"] for a in resp.json()["items"]} == {"p1", "c1"}
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_list_articles_by_child_returns_only_child(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai_llm")
    assert {a["guid"] for a in resp.json()["items"]} == {"c1"}


@pytest.mark.asyncio
async def test_list_articles_genre_exact_excludes_children(client: AsyncClient) -> None:
    """子を持つ親の「まだ子ルールが無いタグの記事」を単独で扱う導線。"""
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai&genre_exact=true")
    assert {a["guid"] for a in resp.json()["items"]} == {"p1"}
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_mark_all_read_by_parent_covers_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post("/api/articles/mark-all-read", json={"genre": "ai"})
    assert resp.json()["marked"] == 2

    listed = await client.get("/api/articles?genre=ai&is_read=false")
    assert listed.json()["total"] == 0


@pytest.mark.asyncio
async def test_mark_all_read_genre_exact_leaves_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post(
        "/api/articles/mark-all-read", json={"genre": "ai", "genre_exact": True}
    )
    assert resp.json()["marked"] == 1

    listed = await client.get("/api/articles?genre=ai_llm&is_read=false")
    assert listed.json()["total"] == 1


@pytest.mark.asyncio
async def test_dismiss_by_parent_covers_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post("/api/articles/dismiss", json={"genre": "ai"})
    assert resp.json()["dismissed"] == 2
    assert len(resp.json()["ids"]) == 2


@pytest.mark.asyncio
async def test_dismiss_genre_exact_leaves_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post(
        "/api/articles/dismiss", json={"genre": "ai", "genre_exact": True}
    )
    assert resp.json()["dismissed"] == 1

    listed = await client.get("/api/articles?genre=ai_llm")
    assert listed.json()["total"] == 1


@pytest.mark.asyncio
async def test_genre_counts_nest_children_and_sum_parent(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")
    await _make_article("c2", "ai_llm")

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["unread_count"] == 3
    assert ai["direct_count"] == 1
    assert [(c["genre"], c["unread_count"]) for c in ai["children"]] == [("ai_llm", 2)]


@pytest.mark.asyncio
async def test_genre_counts_parent_appears_even_with_no_direct_articles(
    client: AsyncClient,
) -> None:
    """代表タグを子に降ろすと親の直下は 0 件になる。それでも親は一覧に出る。"""
    await _seed_hierarchy()
    await _make_article("c1", "ai_llm")

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["direct_count"] == 0
    assert ai["unread_count"] == 1


@pytest.mark.asyncio
async def test_genre_counts_omit_empty_and_sort_desc(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("c1", "ai_llm")
    await _make_article("d1", "dev")
    await _make_article("d2", "dev")

    rows = (await client.get("/api/articles/genres")).json()
    assert [r["genre"] for r in rows] == ["dev", "ai"]
    assert all(r["unread_count"] > 0 for r in rows)
    assert next(r for r in rows if r["genre"] == "dev")["children"] == []


@pytest.mark.asyncio
async def test_genre_counts_keep_reserved_other_at_top_level(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("o1", "other")

    rows = (await client.get("/api/articles/genres")).json()
    other = next(r for r in rows if r["genre"] == "other")
    assert other["label_ja"] == "その他"
    assert other["children"] == []
    assert other["direct_count"] == 1


@pytest.mark.asyncio
async def test_genre_counts_zero_count_sibling_omitted_from_children(
    client: AsyncClient,
) -> None:
    """親に子が 2 つあり、一方が 0 件なら children には非 0 件の子だけが残る。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Genre

    await _seed_hierarchy()
    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        # ai_llm に加えてもう一つ子を作るが、こちらには記事を作らないので 0 件のまま
        session.add(Genre(key="ai_robotics", label_ja="ロボティクス", priority=2, parent_id=parent.id))
        await session.commit()

    await _make_article("c1", "ai_llm")

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert [c["genre"] for c in ai["children"]] == ["ai_llm"]


@pytest.mark.asyncio
async def test_genre_counts_orphan_key_uses_raw_key_as_label(client: AsyncClient) -> None:
    """genres の定義（行）が削除された後も、そのキーが付いた記事はトップレベルに残り、
    ラベルは生のキーになる。API の DELETE ではなく DB を直接触って genres 行だけを消し、
    記事の genre 列は古いキーのまま残す（reclassify_all を経由させない）。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Genre

    await _seed_hierarchy()
    await _make_article("o1", "ai_llm")

    async with async_session() as session:
        child = (await session.execute(select(Genre).where(Genre.key == "ai_llm"))).scalar_one()
        await session.delete(child)
        await session.commit()

    rows = (await client.get("/api/articles/genres")).json()
    orphan = next(r for r in rows if r["genre"] == "ai_llm")
    assert orphan["label_ja"] == "ai_llm"
    assert orphan["children"] == []
    assert orphan["direct_count"] == 1


@pytest.mark.asyncio
async def test_create_child_genre(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")

    resp = await client.post(
        "/api/genres",
        json={"key": "ai_llm", "label_ja": "LLM・生成AI", "priority": 1, "parent_id": parent_id},
    )
    assert resp.status_code == 201
    assert resp.json()["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_cannot_nest_deeper_than_two_levels(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    resp = await client.post(
        "/api/genres",
        json={"key": "ai_llm_rag", "label_ja": "RAG", "priority": 1, "parent_id": child["id"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_set_parent_to_self_or_descendant(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    assert (
        await client.patch(f"/api/genres/{parent_id}", json={"parent_id": parent_id})
    ).status_code == 400
    assert (
        await client.patch(f"/api/genres/{parent_id}", json={"parent_id": child["id"]})
    ).status_code == 400


@pytest.mark.asyncio
async def test_promote_child_to_top_level(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    resp = await client.patch(f"/api/genres/{child['id']}", json={"parent_id": None})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None


@pytest.mark.asyncio
async def test_create_child_404_for_missing_parent(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/genres",
        json={"key": "x", "label_ja": "X", "priority": 1, "parent_id": 99999},
    )
    assert resp.status_code == 404
