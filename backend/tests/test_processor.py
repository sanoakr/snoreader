"""Tests for processor._parse_output."""

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
