"""Tests for existing-tag keyword matching."""

from app.ai.tag_matcher import match_existing_tags
from app.models import Tag


def _tag(name: str, name_ja: str | None = None, id: int = 1) -> Tag:
    return Tag(id=id, name=name, name_ja=name_ja)


def test_title_match_english():
    tags = [_tag("python", "Python", 1), _tag("security", "セキュリティ", 2)]
    result = match_existing_tags(tags, "Python 3.13 released", "")
    assert [t.name for t in result] == ["python"]


def test_title_match_japanese():
    tags = [_tag("security", "セキュリティ", 1)]
    result = match_existing_tags(tags, "最新のセキュリティ動向", "")
    assert [t.name for t in result] == ["security"]


def test_short_ascii_requires_word_boundary():
    # 'ai' should NOT match 'said'
    tags = [_tag("ai", "AI", 1)]
    result = match_existing_tags(tags, "He said hello", "")
    assert result == []


def test_short_ascii_matches_as_word():
    tags = [_tag("ai", "AI", 1)]
    result = match_existing_tags(tags, "GPT-4 is an AI system", "")
    assert [t.name for t in result] == ["ai"]


def test_title_priority_over_body():
    tags = [_tag("body-only", None, 1), _tag("title-match", None, 2)]
    result = match_existing_tags(tags, "title-match release", "body-only content here")
    assert [t.name for t in result] == ["title-match", "body-only"]


def test_short_ascii_matches_adjacent_to_japanese():
    # Python の \b は日本語文字も \w とみなすため、'AIの' のように日本語と
    # 隣接していても ASCII 単語としてはマッチすべき。
    tags = [_tag("ai", "AI", 1), _tag("llm", "LLM", 2), _tag("ocr", "OCR", 3)]
    result = match_existing_tags(tags, "生成AIを取り入れたLLMとOCR活用", "")
    assert {t.name for t in result} == {"ai", "llm", "ocr"}


def test_short_ascii_does_not_match_substring_of_longer_word():
    # 'code' は 'CodeX' の一部として出てきてもマッチしてはいけない。
    tags = [_tag("code", None, 1)]
    result = match_existing_tags(tags, "CodeX のデスクトップアプリを触ってみた", "")
    assert result == []
