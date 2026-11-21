"""参照用要約（kioku が引くための要約）の生成器と出力検証。LLM は常にモックする。"""

from __future__ import annotations

import pytest

from app.ai import reference_summarizer
from app.ai.reference_summarizer import (
    REFERENCE_HEADINGS,
    REFERENCE_MAX_CHARS,
    REFERENCE_MAX_TOKENS,
    REFERENCE_MIN_CHARS,
    current_version_suffix,
    reference_version,
    validate_reference_summary,
)

_FACT_LINES = "".join(f"・事実{i}: Qwen3.8 27B は 2026-11-22 に lethe へ導入された\n" for i in range(8))
VALID = (
    "【要旨】SnoReader の記事。結論は、要約の入力を切り詰めず末尾まで渡す点にある。\n"
    f"【事実】\n{_FACT_LINES}"
    "【主張と出どころ】記事の主張: 切り詰めで結論が落ちる。公式の発表: なし。推測: なし。\n"
    "【時点】2026-11-22 公開。\n"
)


def test_valid_fixture_is_within_length_bounds():
    assert REFERENCE_MIN_CHARS <= len(VALID.strip()) <= REFERENCE_MAX_CHARS
    assert all(h in VALID for h in REFERENCE_HEADINGS)


def test_validate_accepts_four_headings_within_length():
    assert validate_reference_summary(VALID) == VALID.strip()


def test_validate_rejects_missing_heading():
    assert validate_reference_summary(VALID.replace("【時点】", "")) is None


def test_validate_rejects_too_short():
    assert validate_reference_summary("【要旨】a【事実】b【主張と出どころ】c【時点】d") is None


def test_validate_rejects_too_long():
    assert validate_reference_summary(VALID + "あ" * REFERENCE_MAX_CHARS) is None


def test_validate_rejects_think_leak():
    assert validate_reference_summary("<think>下書き</think>\n" + VALID) is None


def test_validate_rejects_none_and_empty():
    assert validate_reference_summary(None) is None
    assert validate_reference_summary("   ") is None


def test_reference_version_format():
    assert reference_version("qwen3.8:27b") == "qwen3.8:27b/ref-v1"
    assert reference_version("qwen3.8:27b").endswith(current_version_suffix())


@pytest.mark.asyncio
async def test_generate_passes_tail_and_published_at(monkeypatch):
    captured: dict = {}

    async def _fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        captured["kwargs"] = kwargs
        return VALID

    monkeypatch.setattr(reference_summarizer, "chat_completion", _fake_chat)
    text = "<p>" + "あ" * 30_000 + "</p><p>結論マーカー</p>"
    result = await reference_summarizer.generate_reference_summary(
        "題", text, "2026-11-20T09:00:00+00:00"
    )
    assert result == VALID.strip()
    assert "結論マーカー" in captured["user"]  # 長文でも末尾が残る
    assert "<p>" not in captured["user"]  # HTML は文章にして渡す
    assert "2026-11-20T09:00:00+00:00" in captured["user"]
    assert captured["kwargs"]["lane"] == "reserved"
    assert captured["kwargs"]["max_tokens"] == REFERENCE_MAX_TOKENS


@pytest.mark.asyncio
async def test_generate_marks_unknown_published_at(monkeypatch):
    captured: dict = {}

    async def _fake_chat(messages, **kwargs):
        captured["user"] = messages[-1]["content"]
        return VALID

    monkeypatch.setattr(reference_summarizer, "chat_completion", _fake_chat)
    await reference_summarizer.generate_reference_summary("題", "本文", None)
    assert "unknown" in captured["user"]


@pytest.mark.asyncio
async def test_generate_returns_none_on_invalid_output(monkeypatch):
    async def _fake_chat(messages, **kwargs):
        return VALID.replace("【時点】", "")

    monkeypatch.setattr(reference_summarizer, "chat_completion", _fake_chat)
    assert await reference_summarizer.generate_reference_summary("題", "本文", None) is None


@pytest.mark.asyncio
async def test_generate_returns_none_when_llm_unavailable(monkeypatch):
    async def _fake_chat(messages, **kwargs):
        return None

    monkeypatch.setattr(reference_summarizer, "chat_completion", _fake_chat)
    assert await reference_summarizer.generate_reference_summary("題", "本文", None) is None
