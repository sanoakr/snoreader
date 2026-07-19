"""Tests for shared AI summary bullet post-processing."""

from app.ai.summary_rules import MAX_SUMMARY_BULLETS, finalize_bullets


def test_passes_through_normal_bullets():
    lines = ["・第一の事実", "・第二の事実"]
    assert finalize_bullets(lines) == "・第一の事実\n・第二の事実"


def test_single_bullet_allowed():
    assert finalize_bullets(["・唯一の事実"]) == "・唯一の事実"


def test_caps_at_max_bullets():
    lines = [f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 5)]
    result = finalize_bullets(lines)
    assert result is not None
    assert result.splitlines() == lines[:MAX_SUMMARY_BULLETS]


def test_drops_tag_annotated_lines():
    lines = ["・正常な要約", "・security|セキュリティ", "・もう一つの事実"]
    assert finalize_bullets(lines) == "・正常な要約\n・もう一つの事実"


def test_empty_input_returns_none():
    assert finalize_bullets([]) is None


def test_all_lines_rejected_returns_none():
    assert finalize_bullets(["・ai|エーアイ"]) is None
