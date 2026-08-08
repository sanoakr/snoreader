"""タグ候補 → ジャンルの決定的な写像のテスト。

分類は DB に触れない純関数なので、ルールを固定値で組んで検証する。
"""

from __future__ import annotations

import pytest

from app.services.genre_classifier import GenreRules, classify, parse_tags


@pytest.fixture
def rules() -> GenreRules:
    return GenreRules(
        tag_to_genre={
            "ai": "ai", "llm": "ai",
            "programming": "dev", "python": "dev",
            "baseball": "sports", "soccer": "sports",
        },
        generic_to_genre={"technology": "dev"},
        priority={"ai": 1, "dev": 3, "sports": 4},
    )


def test_single_tag_maps_to_its_genre(rules: GenreRules):
    assert classify(["python"], rules) == "dev"


def test_multiple_hits_resolve_by_priority(rules: GenreRules):
    """ai(1) と dev(3) に当たる場合、優先順位の小さい ai を採る。"""
    assert classify(["ai", "programming"], rules) == "ai"
    assert classify(["programming", "ai"], rules) == "ai"  # タグの並び順に依存しない


def test_generic_rule_used_when_no_normal_hit(rules: GenreRules):
    assert classify(["technology"], rules) == "dev"


def test_normal_rule_beats_generic_rule(rules: GenreRules):
    """通常ルールが 1 つでもあれば汎用ルールは見ない。"""
    assert classify(["technology", "baseball"], rules) == "sports"


def test_unknown_tags_fall_back_to_other(rules: GenreRules):
    assert classify(["working-holiday", "journey"], rules) == "other"


def test_empty_tags_return_other(rules: GenreRules):
    assert classify([], rules) == "other"


def test_priority_tie_broken_by_key_order():
    """priority が同値でも結果が揺れないこと。"""
    tied = GenreRules(
        tag_to_genre={"alpha": "zeta", "beta": "alpha"},
        generic_to_genre={},
        priority={"zeta": 5, "alpha": 5},
    )
    assert classify(["alpha", "beta"], tied) == "alpha"
    assert classify(["beta", "alpha"], tied) == "alpha"


def test_generic_rules_also_resolve_by_priority():
    """汎用ルールが複数当たる場合も並び順ではなく priority で決める。"""
    multi = GenreRules(
        tag_to_genre={},
        generic_to_genre={"news": "life", "technology": "dev"},
        priority={"dev": 3, "life": 11},
    )
    assert classify(["news", "technology"], multi) == "dev"
    assert classify(["technology", "news"], multi) == "dev"


# --- parse_tags: 構文としては妥当だが list ではない JSON を安全に弾くこと ---
# (tag_suggestions に null / スカラー / dict が入っていてもアプリ起動を落とさない)


def test_parse_tags_none_returns_empty():
    assert parse_tags(None) == []


def test_parse_tags_empty_string_returns_empty():
    assert parse_tags("") == []


def test_parse_tags_json_null_returns_empty():
    assert parse_tags("null") == []


def test_parse_tags_json_scalar_returns_empty():
    assert parse_tags("42") == []
    assert parse_tags('"just a string"') == []


def test_parse_tags_json_dict_returns_empty():
    assert parse_tags('{"a": 1}') == []


def test_parse_tags_malformed_json_returns_empty():
    assert parse_tags("{not valid json") == []


def test_parse_tags_valid_list_passes_through():
    assert parse_tags('["llm", "ai"]') == ["llm", "ai"]


def test_parse_tags_list_with_non_string_items_filters_them():
    assert parse_tags('["llm", 1, null, "ai"]') == ["llm", "ai"]


def test_classify_via_malformed_tag_suggestions_falls_back_to_other(rules: GenreRules):
    """DB から取った tag_suggestions が list でない場合も other に落ちること（reclassify_all 相当の経路）。"""
    for raw in ("null", "42", '{"a": 1}', "{not valid json"):
        assert classify(parse_tags(raw), rules) == "other"
