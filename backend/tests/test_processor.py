"""Tests for processor._parse_output."""

import pytest

from app.ai.processor import _parse_output
from app.ai.summary_rules import MAX_SUMMARY_BULLETS


def test_parse_output_single_bullet_and_tags():
    raw = (
        "SUMMARY:\n"
        "・唯一の結論\n"
        "TAGS: python|Python, security|セキュリティ"
    )
    summary, pairs = _parse_output(raw)
    assert summary == "・唯一の結論"
    assert pairs == [("python", "Python"), ("security", "セキュリティ")]


def test_parse_output_keeps_up_to_nine_bullets():
    bullets = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 4))
    raw = f"SUMMARY:\n{bullets}\nTAGS: news|ニュース"
    summary, _ = _parse_output(raw)
    assert summary is not None
    assert summary.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_parse_output_drops_tag_annotated_bullet():
    raw = (
        "SUMMARY:\n"
        "・正常な要約\n"
        "・security|セキュリティ\n"
        "TAGS: security|セキュリティ"
    )
    summary, pairs = _parse_output(raw)
    assert summary == "・正常な要約"
    assert pairs == [("security", "セキュリティ")]


def test_parse_output_no_valid_bullets_returns_none_summary():
    raw = "SUMMARY:\nTAGS: news|ニュース"
    summary, pairs = _parse_output(raw)
    assert summary is None
    assert pairs == [("news", "ニュース")]


@pytest.mark.asyncio
async def test_summarize_and_tag_passes_article_tail(monkeypatch):
    """12,000 字を超える本文でも、末尾（結論）が LLM への入力に残ること。"""
    from app.ai import processor

    captured: dict = {}

    async def _fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return "SUMMARY:\n・要約\nTAGS: python|Python"

    monkeypatch.setattr(processor, "chat_completion", _fake_chat)
    text = "あ" * 20_000 + "結論マーカー"
    await processor.summarize_and_tag("題", text)
    assert "結論マーカー" in captured["user"]
