"""ジャンルのラベル命名のテスト。LLM は必ずモックする。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_name_genres_parses_one_label_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return "エージェント\nベンチマーク"

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark", "eval")])
    assert labels == ["エージェント", "ベンチマーク"]


@pytest.mark.asyncio
async def test_name_genres_falls_back_to_the_first_tag_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM が落ちても提案は作れなければならない（ラベルは後から編集できる）。"""
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return None

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark", "eval")])
    assert labels == ["agent", "benchmark"]


@pytest.mark.asyncio
async def test_name_genres_pads_a_short_llm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行数が足りない応答でも、戻り値の長さは入力と必ず一致する。"""
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return "エージェント"

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark",)])
    assert labels == ["エージェント", "benchmark"]


@pytest.mark.asyncio
async def test_name_genres_returns_empty_for_no_input() -> None:
    """空入力では LLM を呼ばない。"""
    from app.ai import genre_namer

    assert await genre_namer.name_genres([]) == []
