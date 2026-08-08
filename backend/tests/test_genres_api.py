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
