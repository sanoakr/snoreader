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


def test_split_own_tags_puts_every_bin_under_the_limit() -> None:
    """担当タグが複数ある葉が超過したら、タグを兄弟に分けて全ビンを上限未満にする。"""
    from app.services.genre_split_planner import plan_splits

    # dev_prog が python 30 / rust 30 / api 30 = 90 件。上限 50 を超える
    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "split_own_tags"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "dev_prog"
    assert proposal.before == 90
    assert proposal.projected_max <= 50
    # 新しい兄弟が実際に記事を引き取っている（キーの辞書順で負けていない）
    assert proposal.children
    assert all(c.estimated_unread > 0 for c in proposal.children)
    # 提案されたタグの合計が元の担当タグの部分集合になっている
    proposed_tags = {t for c in proposal.children for t in c.tags}
    assert proposed_tags <= {"python", "rust", "api"}


def test_split_own_tags_children_are_siblings_under_the_same_parent() -> None:
    """新しい子のキーは親キーの接頭辞を持ち、受け皿より辞書順で前になる。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
    )

    proposal = next(
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "split_own_tags"
    )
    for child in proposal.children:
        assert child.key.startswith("dev_")
        assert child.key != "dev_prog"


def test_split_own_tags_projected_max_ignores_an_unrelated_oversized_genre() -> None:
    """無関係なジャンルが上限超過でも、対象ジャンルの提案の projected_max は引き上げられない。"""
    from app.services.genre_split_planner import plan_splits

    # dev_prog は 90 件で分割対象。sports は 500 件で無関係に超過している
    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog", "soccer": "sports"},
        priority={"dev": 3, "dev_prog": 3, "sports": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
        + [(1000 + i, ["soccer"]) for i in range(500)]
    )

    proposal = next(
        p for p in plan_splits(articles, rules, limit=50) if p.genre_key == "dev_prog"
    )
    assert proposal.projected_max <= 50


def test_split_own_tags_rejects_sibling_key_colliding_with_existing_genre() -> None:
    """新しい兄弟キーが既存の無関係なジャンルと衝突するなら提案しない。"""
    from app.services.genre_split_planner import plan_splits

    # rust の受け皿以外の代表タグから作るキーが dev_rust になり、
    # 既存の（無関係な）ジャンル dev_rust と衝突する
    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog", "legacycode": "dev_rust"},
        priority={"dev": 3, "dev_prog": 3, "dev_rust": 1},
        parent={"dev_prog": "dev", "dev_rust": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
        + [(300 + i, ["legacycode"]) for i in range(10)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.genre_key == "dev_prog"]
    assert proposals == []
