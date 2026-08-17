"""推奨サブジャンルの投入テスト。

起動時に自動投入はしない（既存環境では約 15 秒ブロックし、利用者から見れば
「勝手に分類が変わった」になる）。明示操作のエンドポイントとして提供し、
何度押しても差分が出ないことと、利用者が動かしたタグを戻さないことを担保する。
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
async def test_startup_does_not_create_subgenres(client: AsyncClient) -> None:
    """自動投入しないこと。押されるまで階層は増えない。"""
    genres = (await client.get("/api/genres")).json()
    assert all(g["parent_id"] is None for g in genres)


@pytest.mark.asyncio
async def test_seed_creates_children_and_moves_tags(client: AsyncClient) -> None:
    resp = await client.post("/api/genres/seed-subgenres")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 8  # ai 3 + dev 5
    assert body["moved"] > 0

    genres = (await client.get("/api/genres")).json()
    by_key = {g["key"]: g for g in genres}
    ai_id = by_key["ai"]["id"]
    assert by_key["ai_llm"]["parent_id"] == ai_id
    assert by_key["ai_misc"]["parent_id"] == ai_id
    # 代表タグ ai は子へ降りて、親の直下ルールは空になる
    assert [r["tag"] for r in by_key["ai_misc"]["rules"]] == ["ai"]
    assert by_key["ai"]["rules"] == []
    assert by_key["ai"]["generic_rules"] == []
    # technology は汎用ルールのまま子へ移る
    assert [r["tag"] for r in by_key["dev_general"]["generic_rules"]] == ["technology"]
    assert [r["tag"] for r in by_key["dev_general"]["rules"]] == []


@pytest.mark.asyncio
async def test_seed_is_idempotent(client: AsyncClient) -> None:
    first = (await client.post("/api/genres/seed-subgenres")).json()
    second = (await client.post("/api/genres/seed-subgenres")).json()
    assert first["created"] == 8
    assert second == {"created": 0, "moved": 0, "reclassified": 0}


@pytest.mark.asyncio
async def test_seed_does_not_take_back_a_tag_the_user_moved(client: AsyncClient) -> None:
    """利用者が別ジャンルへ移したタグは、対象の親に属していないので触らない。"""
    genres = (await client.get("/api/genres")).json()
    security_id = next(g["id"] for g in genres if g["key"] == "security")
    await client.post(
        "/api/genre-rules", json={"tag": "llm", "genre_id": security_id, "is_generic": False}
    )

    await client.post("/api/genres/seed-subgenres")

    genres = (await client.get("/api/genres")).json()
    by_key = {g["key"]: g for g in genres}
    assert "llm" in [r["tag"] for r in by_key["security"]["rules"]]
    assert "llm" not in [r["tag"] for r in by_key["ai_llm"]["rules"]]


@pytest.mark.asyncio
async def test_seed_reclassifies_existing_articles(client: AsyncClient) -> None:
    import json

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        await session.flush()
        session.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://example.com/a1",
                title="LLM の話",
                summary="",
                genre="ai",
                tag_suggestions=json.dumps(["llm", "ai"]),
            )
        )
        await session.commit()

    resp = await client.post("/api/genres/seed-subgenres")
    assert resp.json()["reclassified"] == 1

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["direct_count"] == 0
    assert [(c["genre"], c["unread_count"]) for c in ai["children"]] == [("ai_llm", 1)]


@pytest.mark.asyncio
async def test_specific_sibling_beats_the_catch_all_child(client: AsyncClient) -> None:
    """受け皿 (ai_misc) は具体的な兄弟 (ai_llm) に負けること。

    兄弟は親と同じ priority を持つので同順位になり、_resolve の同値解決は
    キーの辞書順で決まる。受け皿のキーが兄弟より前に来ると（例えば
    ai_general）、`llm` を持つ記事まで受け皿に吸われて分割の意味が薄れる。
    """
    import json

    from app.database import async_session
    from app.models import Article, Feed
    from app.services.genre_classifier import classify, load_rules, parse_tags

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed2", title="Test Feed 2")
        session.add(feed)
        await session.flush()
        session.add(
            Article(
                feed_id=feed.id,
                guid="b1",
                url="https://example.com/b1",
                title="LLM と AI",
                summary="",
                tag_suggestions=json.dumps(["ai", "llm"]),
            )
        )
        await session.commit()

    await client.post("/api/genres/seed-subgenres")

    async with async_session() as session:
        rules = await load_rules(session)
        assert classify(parse_tags(json.dumps(["ai", "llm"])), rules) == "ai_llm"
        # 受け皿は具体的なタグが無いときだけ使われる
        assert classify(parse_tags(json.dumps(["ai"])), rules) == "ai_misc"
