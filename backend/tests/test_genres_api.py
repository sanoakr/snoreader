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


@pytest.mark.asyncio
async def test_create_genre_rejects_reserved_and_duplicate_key(client: AsyncClient) -> None:
    res = await client.post("/api/genres", json={"key": "other", "label_ja": "その他", "priority": 50})
    assert res.status_code == 400

    res = await client.post("/api/genres", json={"key": "ai", "label_ja": "重複", "priority": 50})
    assert res.status_code == 409

    res = await client.post("/api/genres", json={"key": "hobby", "label_ja": "趣味", "priority": 50})
    assert res.status_code == 201
    assert res.json()["key"] == "hobby"
    # 変更系レスポンスの契約として reclassified を含む（新規ジャンルはルール無しなので 0）
    assert res.json()["reclassified"] == 0


@pytest.mark.asyncio
async def test_rule_moves_between_genres_instead_of_conflicting(client: AsyncClient) -> None:
    """既に他ジャンルにあるタグを送ったら 409 ではなく付け替える。"""
    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")

    res = await client.post("/api/genre-rules", json={"tag": "llm", "genre_id": sports_id, "is_generic": False})
    assert res.status_code == 201

    after = (await client.get("/api/genres")).json()
    tags = {g["key"]: [r["tag"] for r in g["rules"]] for g in after}
    assert "llm" in tags["sports"]
    assert "llm" not in tags["ai"]


@pytest.mark.asyncio
async def test_rule_change_reclassifies_existing_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await reclassify_all(session)
        await session.commit()

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"ai": 1}

    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")
    res = await client.post("/api/genre-rules", json={"tag": "llm", "genre_id": sports_id, "is_generic": False})
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"sports": 1}


@pytest.mark.asyncio
async def test_delete_genre_removes_its_rules_and_reclassifies(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["baseball"])
        await reclassify_all(session)
        await session.commit()

    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")

    res = await client.delete(f"/api/genres/{sports_id}")
    assert res.status_code == 200
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"other": 1}


@pytest.mark.asyncio
async def test_patch_genre_updates_label_and_priority(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    dev_id = next(g["id"] for g in genres if g["key"] == "dev")

    res = await client.patch(f"/api/genres/{dev_id}", json={"label_ja": "開発", "priority": 1})
    assert res.status_code == 200
    assert res.json()["label_ja"] == "開発"

    after = (await client.get("/api/genres")).json()
    assert next(g for g in after if g["key"] == "dev")["priority"] == 1


@pytest.mark.asyncio
async def test_patch_genre_priority_change_moves_article_genre(client: AsyncClient) -> None:
    """dev の priority を ai より小さくすると、両方に当たる記事が ai から dev に移る。"""
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["ai", "programming"])
        await reclassify_all(session)
        await session.commit()

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"ai": 1}

    genres = (await client.get("/api/genres")).json()
    dev_id = next(g["id"] for g in genres if g["key"] == "dev")
    ai_priority = next(g["priority"] for g in genres if g["key"] == "ai")

    res = await client.patch(f"/api/genres/{dev_id}", json={"priority": ai_priority - 1})
    assert res.status_code == 200
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"dev": 1}


@pytest.mark.asyncio
async def test_delete_genre_rule_reclassifies_existing_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import GenreRule
    from app.services.genre_classifier import reclassify_all
    from sqlalchemy import select

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await reclassify_all(session)
        await session.commit()

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"ai": 1}

    async with async_session() as session:
        rule_id = await session.scalar(select(GenreRule.id).where(GenreRule.tag == "llm"))

    res = await client.delete(f"/api/genre-rules/{rule_id}")
    assert res.status_code == 200
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"other": 1}


@pytest.mark.asyncio
async def test_patch_genre_404_for_missing_id(client: AsyncClient) -> None:
    res = await client.patch("/api/genres/999999", json={"label_ja": "x"})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_genre_404_for_missing_id(client: AsyncClient) -> None:
    res = await client.delete("/api/genres/999999")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_delete_genre_rule_404_for_missing_id(client: AsyncClient) -> None:
    res = await client.delete("/api/genre-rules/999999")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_create_genre_rule_404_for_missing_genre(client: AsyncClient) -> None:
    res = await client.post(
        "/api/genre-rules", json={"tag": "foo", "genre_id": 999999, "is_generic": False}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_split_suggestion_endpoints_list_apply_and_dismiss(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案の一覧・適用・無視が API で通ること。LLM 命名はモックする。"""
    import json

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Article, Feed

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")

    async with async_session() as session:
        feed = Feed(title="t", url="http://example.com/feed")
        session.add(feed)
        await session.flush()
        for n in range(60):
            tags = ["ai", "agent"] if n < 30 else ["ai"]
            session.add(
                Article(
                    feed_id=feed.id,
                    guid=f"g{n}",
                    url=f"http://example.com/{n}",
                    title=f"a{n}",
                    tag_suggestions=json.dumps(tags),
                )
            )
        await session.commit()

    refreshed = await client.post("/api/genres/split-suggestions/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["created"] > 0

    listed = await client.get("/api/genres/split-suggestions")
    assert listed.status_code == 200
    items = listed.json()
    assert items
    # projected_max 昇順
    assert [i["projected_max"] for i in items] == sorted(i["projected_max"] for i in items)
    first = items[0]
    assert first["before"] > 50
    assert "children" in first and "demote_tags" in first

    applied = await client.post(f"/api/genres/split-suggestions/{first['id']}/apply", json={})
    assert applied.status_code == 200
    body = applied.json()
    assert set(body) == {"created", "moved", "reclassified"}

    # 適用したら一覧から消える
    after = await client.get("/api/genres/split-suggestions")
    assert all(i["id"] != first["id"] for i in after.json())


@pytest.mark.asyncio
async def test_apply_unknown_suggestion_returns_404(client: AsyncClient) -> None:
    res = await client.post("/api/genres/split-suggestions/9999/apply", json={})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_unknown_suggestion_returns_404(client: AsyncClient) -> None:
    res = await client.post("/api/genres/split-suggestions/9999/dismiss")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_apply_split_suggestions_registered_before_path_param_route(
    client: AsyncClient,
) -> None:
    """/genres/split-suggestions が /genres/{genre_id} より前に登録されていること。

    順序を間違えると FastAPI が "split-suggestions" を genre_id として解釈し、
    パスパラメータの型検証で 422 を返す（404 にはならない）。実際に叩いて確認する。
    """
    res = await client.get("/api/genres/split-suggestions")
    assert res.status_code == 200
    assert res.json() == []  # まだ何も提案していない


@pytest.mark.asyncio
async def test_apply_stale_suggestion_returns_409(client: AsyncClient) -> None:
    """辞書が変わった後に適用すると、子キーの親が食い違って 409 になること。"""
    import json

    from app.database import async_session
    from app.models import GenreSplitSuggestion

    genres = (await client.get("/api/genres")).json()
    dev_id = next(g["id"] for g in genres if g["key"] == "dev")

    # dev の子として conflict_child を先に作っておく（提案の想定と食い違わせる）
    created = await client.post(
        "/api/genres",
        json={"key": "conflict_child", "label_ja": "衝突子", "priority": 10, "parent_id": dev_id},
    )
    assert created.status_code == 201

    # ai を分割する提案を手動で仕込む。子キー conflict_child の親を ai だと想定しているが
    # 実際は dev の子として既に存在するので、適用時に食い違いが検出されるはず
    payload = json.dumps(
        {
            "genre_key": "ai",
            "strategy": "split_own_tags",
            "before": 60,
            "projected_max": 10,
            "children": [
                {
                    "key": "conflict_child",
                    "label_ja": "衝突子",
                    "tags": ["some-foo-tag"],
                    "estimated_unread": 10,
                }
            ],
            "demote_tags": [],
        },
        ensure_ascii=False,
    )
    async with async_session() as session:
        session.add(
            GenreSplitSuggestion(
                genre_key="ai",
                strategy="split_own_tags",
                payload=payload,
                before_count=60,
                projected_max=10,
            )
        )
        await session.commit()

    listed = (await client.get("/api/genres/split-suggestions")).json()
    suggestion_id = next(i["id"] for i in listed if i["genre_key"] == "ai")

    res = await client.post(f"/api/genres/split-suggestions/{suggestion_id}/apply", json={})
    assert res.status_code == 409
    assert "conflict_child" in res.json()["detail"]

    # 何も変更されていないこと（部分適用が残っていない）
    still_listed = (await client.get("/api/genres/split-suggestions")).json()
    assert any(i["id"] == suggestion_id for i in still_listed)


@pytest.mark.asyncio
async def test_list_articles_filters_by_genre(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"], title="AI の記事")
        await _make_article(session, feed.id, "g2", ["baseball"], title="野球の記事")
        await reclassify_all(session)
        await session.commit()

    res = await client.get("/api/articles", params={"genre": "ai"})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "AI の記事"
