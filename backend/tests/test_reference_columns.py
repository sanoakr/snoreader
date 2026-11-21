"""reference_summary 系の列が、旧スキーマの既存 DB にも ALTER TABLE で足されること。"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

REFERENCE_COLUMNS = (
    "reference_summary",
    "reference_summary_at",
    "reference_summary_version",
    "reference_basis",
)


def _article_columns(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(articles)")}
    finally:
        con.close()


@pytest.mark.asyncio
async def test_lifespan_adds_reference_columns_to_existing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    async def _run_lifespan() -> None:
        from app import database as database_module

        importlib.reload(database_module)
        from app import main as main_module

        importlib.reload(main_module)
        async with main_module.lifespan(main_module.app):
            pass
        await database_module.engine.dispose()

    # 1 回目: 現行スキーマで DB を作る。そこから 4 列を落として旧スキーマにする
    await _run_lifespan()
    con = sqlite3.connect(db_path)
    try:
        for col in REFERENCE_COLUMNS:
            if col in _article_columns(db_path):
                con.execute(f"ALTER TABLE articles DROP COLUMN {col}")
        con.commit()
    finally:
        con.close()
    assert not set(REFERENCE_COLUMNS) & _article_columns(db_path)

    # 2 回目: 旧スキーマの DB に対して lifespan が列を足すこと
    await _run_lifespan()
    assert set(REFERENCE_COLUMNS) <= _article_columns(db_path)
