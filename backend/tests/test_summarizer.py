"""Tests for summarizer._clean_summary."""

import pytest

from app.ai.summarizer import _clean_summary
from app.ai.summary_rules import MAX_SUMMARY_BULLETS


def test_clean_summary_keeps_single_bullet():
    assert _clean_summary("・唯一の結論") == "・唯一の結論"


def test_clean_summary_keeps_up_to_nine_bullets():
    raw = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS))
    result = _clean_summary(raw)
    assert result is not None
    assert result.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_clean_summary_caps_beyond_nine():
    raw = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 3))
    result = _clean_summary(raw)
    assert result is not None
    assert result.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_clean_summary_ignores_non_bullet_lines():
    raw = "前置き\n・唯一の結論\n後書き"
    assert _clean_summary(raw) == "・唯一の結論"


def test_clean_summary_returns_none_when_no_bullets():
    assert _clean_summary("no bullets here") is None


@pytest.mark.asyncio
async def test_summarize_article_passes_article_tail(monkeypatch):
    """12,000 字を超える本文でも、末尾（結論）が LLM への入力に残ること。"""
    from app.ai import summarizer

    captured: dict = {}

    async def _fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "・要約"

    monkeypatch.setattr(summarizer, "chat_completion", _fake_chat)
    text = "あ" * 20_000 + "結論マーカー"
    await summarizer.summarize_article("題", text)
    assert "結論マーカー" in captured["user"]
