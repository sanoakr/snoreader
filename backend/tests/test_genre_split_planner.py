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


def test_split_own_tags_rejects_a_phantom_child_that_ties_the_receptacle() -> None:
    """1 件も引き取れない子が混ざる案は、他の全ガードを通っても丸ごと棄却される。

    python が受け皿（30 件 = python 単独 10 件 + python+rust 20 件）。api は
    30 件で綺麗に dev_api へ移り、それだけで dev_prog は 30 件（上限未満）に
    下がる——つまり estimated_unread == 0 ガード以外の全チェック
    （projected[genre_key] <= limit、projected_max <= limit、キー衝突なし）は
    すでに通っている。しかし rust 付きの記事は必ず python も持つため、
    dev_rust は dev_prog と同じ priority でタイになり、辞書順で
    "dev_prog" < "dev_rust" のため必ず負ける（0 件）。この phantom な
    dev_rust が混ざっている以上、案（dev_api の分も含めて）は丸ごと
    棄却されなければならない。

    直後の test_split_own_tags_succeeds_once_the_phantom_tag_is_removed が、
    rust タグだけを外せば同じ rules で普通に split_own_tags が成立することを
    示す——つまりここで [] になるのは「セットアップ自体が壊れているから」
    ではなく「phantom な子が混ざっているから」であることを裏付ける
    （この裏付けが無いと、このテストは何か無関係な理由で棄却されても
    同じように通ってしまい、estimated_unread == 0 ガードを検出できない）。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"python": "dev_prog", "api": "dev_prog", "rust": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(10)]
        + [(100 + i, ["python", "rust"]) for i in range(20)]
        + [(200 + i, ["api"]) for i in range(30)]
    )

    assert plan_splits(articles, rules, limit=50) == []


def test_split_own_tags_succeeds_once_the_phantom_tag_is_removed() -> None:
    """上のテストと同じ rules・同じ記事総数で rust タグだけを外すと、普通に
    split_own_tags が成立する。これにより上のテストの [] が「セットアップが
    壊れているから」ではないことを確認できる。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"python": "dev_prog", "api": "dev_prog", "rust": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    # rust タグを外した以外は上のテストと同じ記事構成（python 30 件 + api 30 件）
    articles = [(i, ["python"]) for i in range(30)] + [(200 + i, ["api"]) for i in range(30)]

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "split_own_tags"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "dev_prog"
    assert proposal.children
    assert all(c.estimated_unread > 0 for c in proposal.children)


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
    記事を 1 件も取れない。シミュレーションがこれを projected 0 として検出し、
    estimated_unread == 0 の子を含む案は丸ごと棄却されるべき。

    misc_mid が受け皿。law は辞書順で misc_mid の前に来るので正しく勝ち、
    war は後に来るので必ず負ける（law だけ抜けば misc_mid は上限未満になる）。
    news_zzz は無関係な対照ジャンル: sports だけが未ルール共起タグで、誰も
    負けないので常に成立する——plan_splits がここから最低 1 件は案を返すこと
    を保証し、この下のループが空で回って何も検証しない事態を防ぐ
    （元のテストは articles を変えても plan_splits が [] を返すままだったため、
    ループ本体が一度も実行されず、estimated_unread == 0 棄却ガードを両方の
    プランナから削除しても全テストが通ってしまっていた）。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"topic": "misc_mid", "headline": "news_zzz"},
        priority={"misc": 5, "misc_mid": 5, "news": 2, "news_zzz": 2},
        parent={"misc_mid": "misc", "news_zzz": "news"},
    )
    articles = (
        [(i, ["topic", "law"]) for i in range(20)]          # 受け皿より前: 正しく勝つ
        + [(100 + i, ["topic", "war"]) for i in range(20)]  # 受け皿より後: 必ず負ける
        + [(200 + i, ["topic"]) for i in range(15)]         # topic だけ（受け皿に残る）
        + [(300 + i, ["headline", "sports"]) for i in range(20)]  # 対照: 常に勝つ
        + [(400 + i, ["headline"]) for i in range(40)]
    )

    proposals = plan_splits(articles, rules, limit=50)
    assert proposals  # ループが空で回って何も検証しない、を防ぐ
    for proposal in proposals:
        # 提案されたどの子も、必ず 1 件以上引き取れている
        assert all(c.estimated_unread > 0 for c in proposal.children)


def test_strategy_ties_break_in_spec_table_order() -> None:
    """projected_max が同値なら、仕様の表の順（C: demote_generic -> A: split_own_tags
    -> B: promote_free_tags）で並ぶ。文字列のアルファベット順に流されると
    ("promote_free_tags" < "split_own_tags") A と B の順が入れ替わってしまう。
    """
    from app.services.genre_split_planner import plan_splits

    # dev_prog は python 40 件（すべて docker も併せ持つ）+ rust 20 件 = 60 件で超過。
    # split_own_tags は rust を追い出して projected_max=40。
    # promote_free_tags は docker を追い出して projected_max=40。ちょうど同値になる。
    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python", "docker"]) for i in range(40)]
        + [(100 + i, ["rust"]) for i in range(20)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.genre_key == "dev_prog"]
    assert proposals  # 空リストでは同値タイの主張自体が検証されない
    # 実際に同値であることを確認しておく（同値でなければ順序の主張自体が無意味）
    assert len({p.projected_max for p in proposals}) == 1
    assert [p.strategy for p in proposals] == ["split_own_tags", "promote_free_tags"]


def test_promote_free_tags_splits_a_childless_top_level_genre() -> None:
    """子を持たない親ジャンルが超過したときも、その下に子を新設できる。

    politics（子なしトップレベル、priority 6, 担当タグ government）が本番の
    政治ジャンル（現在 37 件）を縮めた再現。conflict は未ルールの共起タグで、
    新兄弟 politics_conflict の priority は親と同じ 6 になるため必ず同順位になる。
    parent を _simulate に登録しないと、キー文字列の接頭辞比較で親 "politics" が
    常に子 "politics_conflict" に勝ってしまい、子が 0 件になって提案が棄却される。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules({"government": "politics"}, priority={"politics": 6})
    articles = (
        [(i, ["government", "conflict"]) for i in range(30)]
        + [(100 + i, ["government"]) for i in range(30)]
    )

    proposals = [
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"
    ]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "politics"
    assert proposal.before == 60
    assert proposal.projected_max <= 50
    assert {c.key for c in proposal.children} == {"politics_conflict"}
    assert all(c.estimated_unread > 0 for c in proposal.children)
