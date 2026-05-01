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
