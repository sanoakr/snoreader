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


def test_demote_generic_shrinks_a_receptacle_genre_without_adding_children() -> None:
    """担当タグが 1 つの受け皿が超過したら、そのタグを汎用に降格して他ジャンルに譲る。

    実データの ai_misc（担当タグは ai だけ、53 件）を縮めた再現。ai の priority が
    最小なので、ai + security の記事も ai_misc に落ちてしまっている。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"ai": "ai_misc", "llm": "ai_llm", "security": "security", "python": "dev"},
        priority={"ai": 1, "ai_misc": 1, "ai_llm": 1, "security": 2, "dev": 3},
        parent={"ai_misc": "ai", "ai_llm": "ai"},
    )
    # ai だけの記事 20 件 + ai と他ジャンルタグを併せ持つ記事 40 件 = ai_misc に 60 件
    articles = (
        [(i, ["ai"]) for i in range(20)]
        + [(100 + i, ["ai", "security"]) for i in range(20)]
        + [(200 + i, ["ai", "python"]) for i in range(20)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "demote_generic"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "ai_misc"
    assert proposal.before == 60
    assert proposal.demote_tags == ("ai",)
    assert proposal.children == ()          # ジャンルを増やさない手
    assert proposal.projected_max <= 50


def test_promote_free_tags_creates_siblings_from_unruled_cooccurring_tags() -> None:
    """未ルールの共起タグを新しい兄弟の担当タグにする。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"ai": "ai_misc"},
        priority={"ai": 1, "ai_misc": 1},
        parent={"ai_misc": "ai"},
    )
    # agent 20 件 / benchmark 15 件 は未ルール。残り 25 件は ai だけ
    articles = (
        [(i, ["ai", "agent"]) for i in range(20)]
        + [(100 + i, ["ai", "benchmark"]) for i in range(15)]
        + [(200 + i, ["ai"]) for i in range(25)]
    )

    proposals = [
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"
    ]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.before == 60
    assert proposal.projected_max <= 50
    assert {t for c in proposal.children for t in c.tags} == {"agent", "benchmark"}
    assert all(c.key.startswith("ai_") for c in proposal.children)


def test_promote_free_tags_ignores_tags_below_the_minimum_article_count() -> None:
    """2 件級の未ルールタグでジャンルを作らない（下限 _MIN_CHILD_ARTICLES）。

    実データの ai_misc の未ルール共起タグは waymo 2 / google 2 しかなく、
    下限がないと 2 件のジャンルが量産される。
    """
    from app.services.genre_split_planner import _MIN_CHILD_ARTICLES, plan_splits

    assert _MIN_CHILD_ARTICLES > 2
    rules = _rules({"ai": "ai_misc"}, priority={"ai": 1, "ai_misc": 1}, parent={"ai_misc": "ai"})
    articles = (
        [(i, ["ai", "waymo"]) for i in range(2)]
        + [(100 + i, ["ai", "google"]) for i in range(2)]
        + [(200 + i, ["ai"]) for i in range(60)]
    )

    assert [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"] == []


def test_promote_free_tags_on_other_creates_top_level_genres() -> None:
    """other は genres に行がなくぶら下げ先がないので、新しいトップレベルを提案する。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules({"python": "dev"}, priority={"dev": 3})
    # どのルールにも当たらない記事 60 件。football 30 / drone 30
    articles = (
        [(i, ["football"]) for i in range(30)]
        + [(100 + i, ["drone"]) for i in range(30)]
    )

    proposals = [
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"
    ]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "other"
    assert {c.key for c in proposal.children} == {"football", "drone"}
    assert proposal.projected_max <= 50


def test_a_sibling_key_sorting_after_the_receptacle_is_rejected() -> None:
    """受け皿より辞書順で後になる兄弟キーの案は棄却される。

    兄弟は親と同じ priority を持つので必ず同順位になり、_resolve の同値解決
    （キーの辞書順）で決まる。受け皿より後にソートされるキーの新兄弟は
    記事を 1 件も取れない。シミュレーションがこれを projected 0 で検出する。
    """
    from app.services.genre_split_planner import plan_splits

    # 受け皿は ai_aaa（辞書順で最初）。新兄弟 ai_zzz は必ず負ける
    rules = _rules({"ai": "ai_aaa"}, priority={"ai": 1, "ai_aaa": 1}, parent={"ai_aaa": "ai"})
    articles = (
        [(i, ["ai", "zzz"]) for i in range(30)]
        + [(100 + i, ["ai"]) for i in range(30)]
    )

    for proposal in plan_splits(articles, rules, limit=50):
        # 提案されたどの子も、必ず 1 件以上引き取れている
        assert all(c.estimated_unread > 0 for c in proposal.children)
