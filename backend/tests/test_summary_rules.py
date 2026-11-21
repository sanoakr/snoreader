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


from app.ai.summary_rules import CLIP_SEPARATOR, clip_for_llm  # noqa: E402


def test_clip_returns_text_unchanged_at_exact_limit():
    text = "あ" * 100
    assert clip_for_llm(text, 100) == text


def test_clip_keeps_head_and_tail_when_over_limit():
    text = "H" * 200 + "M" * 100 + "T" * 200  # 500 字
    out = clip_for_llm(text, 300)
    assert out.startswith("H" * 200)  # 先頭 limit*2//3 = 200
    assert out.endswith("T" * 100)  # 末尾 limit//3 = 100
    assert CLIP_SEPARATOR in out
    assert "M" not in out
    assert len(out) == 300 + len(CLIP_SEPARATOR)
