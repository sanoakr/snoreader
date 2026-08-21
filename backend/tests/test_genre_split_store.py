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
