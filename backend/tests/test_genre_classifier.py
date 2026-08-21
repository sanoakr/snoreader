"""タグ候補 → ジャンルの決定的な写像のテスト。

分類は DB に触れない純関数なので、ルールを固定値で組んで検証する。
load_rules() だけは DB から組み立てる側なので、そこだけ DB 付きで検証する。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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


@pytest.fixture
def hierarchical_rules() -> GenreRules:
    """ai(親) の下に ai_llm(子)、dev(親) の下に dev_general(子・汎用) を置いた構成。"""
    return GenreRules(
        tag_to_genre={
            "ai": "ai",            # 親を指す代表タグ
            "llm": "ai_llm",       # 子を指すタグ
            "programming": "dev",
            "baseball": "sports",
        },
        generic_to_genre={"technology": "dev_general"},
        priority={"ai": 1, "ai_llm": 1, "dev": 3, "dev_general": 3, "sports": 4},
        parent={"ai_llm": "ai", "dev_general": "dev"},
    )


def test_descendant_beats_ancestor(hierarchical_rules: GenreRules):
    """代表タグ(ai)と子タグ(llm)が両方当たったら子を採る。

    priority の手動調整に頼ると、代表タグを持つ記事が親に残り続けて分割されない。
    """
    assert classify(["ai", "llm"], hierarchical_rules) == "ai_llm"
    assert classify(["llm", "ai"], hierarchical_rules) == "ai_llm"


def test_parent_kept_when_no_child_rule_hits(hierarchical_rules: GenreRules):
    """子ルールが無いタグの記事は親に残る（親自身の束になる）。"""
    assert classify(["ai"], hierarchical_rules) == "ai"


def test_unrelated_genres_still_resolve_by_priority(hierarchical_rules: GenreRules):
    """祖先・子孫の関係が無い候補どうしは従来通り priority で決まる。"""
    assert classify(["ai", "programming"], hierarchical_rules) == "ai"
    assert classify(["baseball", "programming"], hierarchical_rules) == "dev"


def test_child_genre_wins_over_unrelated_higher_priority(hierarchical_rules: GenreRules):
    """子に降ろしても、親と同じ priority を与えていれば他ジャンルとの優劣は変わらない。"""
    assert classify(["llm", "programming"], hierarchical_rules) == "ai_llm"


def test_generic_stage_also_prunes_ancestors():
    """汎用ルールの段でも子孫優先が効くこと。"""
    rules = GenreRules(
        tag_to_genre={},
        generic_to_genre={"technology": "dev_general", "news": "dev"},
        priority={"dev": 3, "dev_general": 3},
        parent={"dev_general": "dev"},
    )
    assert classify(["technology", "news"], rules) == "dev_general"


def test_other_is_not_part_of_the_hierarchy(hierarchical_rules: GenreRules):
    assert classify(["unknown-tag"], hierarchical_rules) == "other"


# --- load_rules: DB から priority を組み立てる側の回帰テスト ---
# (test_genres_api.py / test_subgenre_seed.py と同じ lifespan 付き client 作法)


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_load_rules_uses_the_real_priority_for_a_rule_less_genre(
    client: AsyncClient,
) -> None:
    """ルールを 1 つも持たない genre でも、priority は Genre.priority の実値になる。

    ルールが 1 行も無い genre は tag_to_genre / generic_to_genre 経由では
    絶対に priority を得られない（select が GenreRule -> Genre の join なので）。
    そのフォールバックがセンチネル (_FALLBACK_PRIORITY) を返すと、
    genre_split_planner が新しい兄弟に「親と同じ priority」を継がせる際、
    親（この genre）がルールを失った途端に新しい兄弟が既存兄弟とのタイブレークに
    必ず負けて 0 件案として棄却されてしまう（本タスクで見つけた実バグ）。
    """
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Genre, GenreRule
    from app.services.genre_classifier import _FALLBACK_PRIORITY, load_rules

    async with async_session() as session:
        ruled = Genre(key="ruled_genre", label_ja="ルール有り", priority=5)
        # seed-subgenres が親の全タグを子へ譲るのと同じ状況: genres に行はあるが
        # genre_rules は 1 行も無い
        ruleless = Genre(key="ruleless_genre", label_ja="ルール無し", priority=7)
        session.add_all([ruled, ruleless])
        await session.flush()
        session.add(GenreRule(tag="ruled-tag", genre_id=ruled.id, is_generic=False))
        await session.commit()

    async with async_session() as session:
        rules = await load_rules(session)
        # 念のため DB 上の実値と一致することも確認する
        stored_priority = await session.scalar(
            select(Genre.priority).where(Genre.key == "ruleless_genre")
        )

    assert rules.priority["ruled_genre"] == 5  # 従来通り、ルール経由で得られる
    assert rules.priority["ruleless_genre"] == 7  # センチネルではなく実値
    assert rules.priority["ruleless_genre"] != _FALLBACK_PRIORITY
    assert rules.priority["ruleless_genre"] == stored_priority
