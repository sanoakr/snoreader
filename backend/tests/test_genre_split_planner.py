"""ジャンル分割プランナのテスト。DB も LLM も使わない純関数テスト。"""

from __future__ import annotations

from app.services.genre_classifier import GenreRules


def _rules(
    tag_to_genre: dict[str, str],
    *,
    generic: dict[str, str] | None = None,
    priority: dict[str, int] | None = None,
    parent: dict[str, str] | None = None,
) -> GenreRules:
    """テスト用の GenreRules を組む。priority 未指定のジャンルは 100 とする。"""
    keys = set(tag_to_genre.values()) | set((generic or {}).values())
    keys |= set((parent or {}).keys()) | set((parent or {}).values())
    prio = {k: 100 for k in keys}
    prio.update(priority or {})
    return GenreRules(dict(tag_to_genre), dict(generic or {}), prio, dict(parent or {}))


def test_no_proposal_when_every_genre_is_under_the_limit() -> None:
    """上限以下しかないときは提案を出さない。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules({"python": "dev", "soccer": "sports"})
    articles = [(i, ["python"]) for i in range(5)] + [(100 + i, ["soccer"]) for i in range(5)]

    assert plan_splits(articles, rules, limit=50) == []
