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
