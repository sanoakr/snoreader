"""分割提案の保存・適用・無視のテスト。LLM は必ずモックする。"""

from __future__ import annotations

import pytest


def test_settings_expose_the_unread_limit() -> None:
    from app.config import Settings

    assert Settings().genre_unread_limit == 50


def test_settings_read_the_limit_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("SNOREADER_GENRE_UNREAD_LIMIT", "30")
    assert Settings().genre_unread_limit == 30


def test_suggestion_model_columns_exist() -> None:
    from app.models import GenreSplitSuggestion

    columns = set(GenreSplitSuggestion.__table__.columns.keys())
    assert columns == {
        "id",
        "genre_key",
        "strategy",
        "payload",
        "before_count",
        "projected_max",
        "created_at",
        "dismissed_at",
        "dismissed_at_count",
    }


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """test_genres_api.py と同じ作法。lifespan を通して DB を初期化する。"""
    import importlib

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


async def _make_articles(genre_tags: list[tuple[str, int]]) -> None:
    """(タグ JSON, 件数) の指定で未読記事を作る。

    同じテスト内で複数回呼べるように、フィード URL と記事 guid を毎回
    一意にする（brief のヘルパーは 1 テスト 1 回呼びが前提だったが、
    再提案の「未読が増えた」ケースを検証するには追加呼び出しが必要）。
    """
    import json
    import uuid

    from app.database import async_session
    from app.models import Article, Feed

    call_id = uuid.uuid4().hex
    async with async_session() as session:
        feed = Feed(title="t", url=f"http://example.com/feed-{call_id}")
        session.add(feed)
        await session.flush()
        n = 0
        for tags, count in genre_tags:
            for _ in range(count):
                n += 1
                session.add(
                    Article(
                        feed_id=feed.id,
                        guid=f"g{n}",
                        url=f"http://example.com/{n}",
                        title=f"a{n}",
                        tag_suggestions=json.dumps(json.loads(tags)),
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_stores_a_proposal_for_an_over_limit_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ai タグ 60 件で ai_misc が上限超になり、提案が保存される。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    # seed_genres + seed_subgenres 後の辞書で ai -> ai_misc になる
    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        created = await refresh_split_suggestions(session)
        await session.commit()
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()

    assert created > 0
    assert any(r.genre_key == "ai_misc" for r in rows)
    assert all(r.dismissed_at is None for r in rows)
    assert all(r.projected_max <= 50 for r in rows)


@pytest.mark.asyncio
async def test_refresh_is_idempotent_while_a_proposal_is_pending(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留中の同じ提案があれば二重に作らない（毎時間つつかない）。"""
    from sqlalchemy import func, select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()
    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(GenreSplitSuggestion))

    assert second == 0
    assert total is not None and total > 0


@pytest.mark.asyncio
async def test_refresh_makes_no_proposal_when_all_genres_are_small(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai import genre_namer
    from app.database import async_session
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await _make_articles([('["ai"]', 5), ('["python"]', 5)])

    async with async_session() as session:
        assert await refresh_split_suggestions(session) == 0
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_suppresses_a_dismissed_proposal_while_unread_count_is_unchanged(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視済みの提案は、無視した時点と同じ未読件数のままなら再提案しない。

    保留中の抑制（同じ (genre_key, strategy) の行がある）とは別の規則である点に注意:
    ここでは行を dismissed_at 済みにした上で、件数がまだ dismissed_at_count を
    超えていないケースを検証する。
    """
    from sqlalchemy import func, select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        first = await refresh_split_suggestions(session)
        await session.commit()
    assert first > 0

    # 生成された提案をすべて「無視済み」にする。件数はそのとき記録された
    # before_count と同じ値にする（＝未読は増えていない）
    async with async_session() as session:
        rows = (
            await session.execute(select(GenreSplitSuggestion))
        ).scalars().all()
        for row in rows:
            row.dismissed_at = "2026-08-21T00:00:00"
            row.dismissed_at_count = row.before_count
        await session.commit()

    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(GenreSplitSuggestion))

    assert second == 0
    assert total == len(rows)


@pytest.mark.asyncio
async def test_refresh_reproposes_a_dismissed_genre_once_unread_count_grows(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視済みでも、未読が dismissed_at_count を超えて増えたら再提案する。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        first = await refresh_split_suggestions(session)
        await session.commit()
    assert first > 0

    async with async_session() as session:
        rows = (
            await session.execute(select(GenreSplitSuggestion))
        ).scalars().all()
        for row in rows:
            row.dismissed_at = "2026-08-21T00:00:00"
            row.dismissed_at_count = row.before_count
        await session.commit()

    # 未読を増やして dismissed_at_count を超えさせる
    await _make_articles([('["ai"]', 20)])

    async with async_session() as session:
        third = await refresh_split_suggestions(session)
        await session.commit()

    assert third > 0
