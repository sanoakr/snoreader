"""分割提案の保存・適用・無視のテスト。LLM は必ずモックする。"""

from __future__ import annotations

import pytest


def test_settings_expose_the_unread_limit() -> None:
    from app.config import Settings

    assert Settings().genre_unread_limit == 50


def test_settings_read_the_limit_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("SNOREADER_GENRE_UNREAD_LIMIT", "30")
    assert Settings().genre_unread_limit == 30


def test_suggestion_model_columns_exist() -> None:
    from app.models import GenreSplitSuggestion

    columns = set(GenreSplitSuggestion.__table__.columns.keys())
    assert columns == {
        "id",
        "genre_key",
        "strategy",
        "payload",
        "before_count",
        "projected_max",
        "created_at",
        "dismissed_at",
        "dismissed_at_count",
    }


import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """test_genres_api.py と同じ作法。lifespan を通して DB を初期化する。"""
    import importlib

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


async def _make_articles(genre_tags: list[tuple[str, int]]) -> None:
    """(タグ JSON, 件数) の指定で未読記事を作る。

    同じテスト内で複数回呼べるように、フィード URL と記事 guid を毎回
    一意にする（brief のヘルパーは 1 テスト 1 回呼びが前提だったが、
    再提案の「未読が増えた」ケースを検証するには追加呼び出しが必要）。
    """
    import json
    import uuid

    from app.database import async_session
    from app.models import Article, Feed

    call_id = uuid.uuid4().hex
    async with async_session() as session:
        feed = Feed(title="t", url=f"http://example.com/feed-{call_id}")
        session.add(feed)
        await session.flush()
        n = 0
        for tags, count in genre_tags:
            for _ in range(count):
                n += 1
                session.add(
                    Article(
                        feed_id=feed.id,
                        guid=f"g{n}",
                        url=f"http://example.com/{n}",
                        title=f"a{n}",
                        tag_suggestions=json.dumps(json.loads(tags)),
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_stores_a_proposal_for_an_over_limit_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ai タグ 60 件で ai_misc が上限超になり、提案が保存される。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    # seed_genres + seed_subgenres 後の辞書で ai -> ai_misc になる
    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        created = await refresh_split_suggestions(session)
        await session.commit()
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()

    assert created > 0
    assert any(r.genre_key == "ai_misc" for r in rows)
    assert all(r.dismissed_at is None for r in rows)
    assert all(r.projected_max <= 50 for r in rows)


@pytest.mark.asyncio
async def test_refresh_is_idempotent_while_a_proposal_is_pending(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留中の同じ提案があれば二重に作らない（毎時間つつかない）。"""
    from sqlalchemy import func, select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()
    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(GenreSplitSuggestion))

    assert second == 0
    assert total is not None and total > 0


@pytest.mark.asyncio
async def test_refresh_makes_no_proposal_when_all_genres_are_small(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai import genre_namer
    from app.database import async_session
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await _make_articles([('["ai"]', 5), ('["python"]', 5)])

    async with async_session() as session:
        assert await refresh_split_suggestions(session) == 0
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_suppresses_a_dismissed_proposal_while_unread_count_is_unchanged(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視済みの提案は、無視した時点と同じ未読件数のままなら再提案しない。

    保留中の抑制（同じ (genre_key, strategy) の行がある）とは別の規則である点に注意:
    ここでは行を dismissed_at 済みにした上で、件数がまだ dismissed_at_count を
    超えていないケースを検証する。
    """
    from sqlalchemy import func, select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        first = await refresh_split_suggestions(session)
        await session.commit()
    assert first > 0

    # 生成された提案をすべて「無視済み」にする。件数はそのとき記録された
    # before_count と同じ値にする（＝未読は増えていない）
    async with async_session() as session:
        rows = (
            await session.execute(select(GenreSplitSuggestion))
        ).scalars().all()
        for row in rows:
            row.dismissed_at = "2026-08-21T00:00:00"
            row.dismissed_at_count = row.before_count
        await session.commit()

    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(GenreSplitSuggestion))

    assert second == 0
    assert total == len(rows)


@pytest.mark.asyncio
async def test_refresh_reproposes_a_dismissed_genre_once_unread_count_grows(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視済みでも、未読が dismissed_at_count を超えて増えたら再提案する。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        first = await refresh_split_suggestions(session)
        await session.commit()
    assert first > 0

    async with async_session() as session:
        rows = (
            await session.execute(select(GenreSplitSuggestion))
        ).scalars().all()
        for row in rows:
            row.dismissed_at = "2026-08-21T00:00:00"
            row.dismissed_at_count = row.before_count
        await session.commit()

    # 未読を増やして dismissed_at_count を超えさせる
    await _make_articles([('["ai"]', 20)])

    async with async_session() as session:
        third = await refresh_split_suggestions(session)
        await session.commit()

    assert third > 0


@pytest.mark.asyncio
async def test_refresh_dismissed_floor_uses_the_max_across_multiple_dismissed_rows(
    client: AsyncClient,
) -> None:
    """同じ (genre_key, strategy) の無視済み行が複数あるとき、閾値は最大値を使う。

    「最後の行が勝つ」実装だと、後から挿入した行の dismissed_at_count が
    たまたま小さいときに再提案が early に起きてしまう。それを検出するため、
    未読件数 (55) を 2 つの閾値の間 (40 < 55 < 100) に置く: 正しい max() 実装
    なら 55 <= max(100, 40) = 100 で抑制されるが、「最後の行が勝つ」実装なら
    最後に挿入した行の 40 を使ってしまい 55 > 40 で誤って再提案してしまう。
    """
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    # どちらのルールにも当たらないタグを 2 種類使う。1 種類だけだと
    # promote_free_tags が全件を 1 つの新ジャンルに詰め込むだけになり、
    # 詰め込んだ先も上限を超えて棄却される（分割の意味がない）。2 種類に
    # 分けることで初めて "other" が上限超・分割後は両方が上限内という
    # 本物の promote_free_tags 案が成立し、before=55 になる
    await _make_articles([('["zzzalpha"]', 30), ('["zzzbeta"]', 25)])

    async with async_session() as session:
        # 同じ (genre_key, strategy) について、count が異なる無視済み行を 2 つ直接挿入する。
        # 閾値が低いほうの行を最後に挿入する（「最後の行が勝つ」実装だとそちらを
        # 使ってしまい、この後の未読件数 55 で誤って再提案してしまう）
        session.add(
            GenreSplitSuggestion(
                genre_key="other",
                strategy="promote_free_tags",
                payload='{"genre_key": "other", "strategy": "promote_free_tags", '
                '"before": 55, "projected_max": 55, "children": [], "demote_tags": []}',
                before_count=55,
                projected_max=55,
                dismissed_at="2026-08-20T00:00:00+00:00",
                dismissed_at_count=100,
            )
        )
        session.add(
            GenreSplitSuggestion(
                genre_key="other",
                strategy="promote_free_tags",
                payload='{"genre_key": "other", "strategy": "promote_free_tags", '
                '"before": 55, "projected_max": 55, "children": [], "demote_tags": []}',
                before_count=55,
                projected_max=55,
                dismissed_at="2026-08-21T00:00:00+00:00",
                dismissed_at_count=40,
            )
        )
        await session.commit()

    # 現在の未読 (55) は高いほうの閾値 (100) を超えていないので、max(100, 40) = 100
    # を使えば再提案は起きないはずである（55 は低いほうの閾値 40 は超えている）
    async with async_session() as session:
        created = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(
            select(func.count()).select_from(GenreSplitSuggestion)
        )

    assert created == 0
    assert total == 2  # 新しい行が作られていない


@pytest.mark.asyncio
async def test_refresh_dismissed_floor_falls_back_to_before_count_when_count_is_null(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dismissed_at_count が NULL の無視済み行は before_count を閾値にする。

    未読件数がその値と同じままなら再提案せず、それを超えたら再提案する。
    """
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        first = await refresh_split_suggestions(session)
        await session.commit()
    assert first > 0

    # dismissed_at_count を明示的に NULL にしたまま無視済みにする
    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        for row in rows:
            row.dismissed_at = "2026-08-21T00:00:00+00:00"
            row.dismissed_at_count = None
        await session.commit()

    # before_count と同じ未読件数のままなら再提案されない（NULL は before_count に落ちる）
    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
    assert second == 0

    # before_count を超えるまで未読を増やすと再提案される
    await _make_articles([('["ai"]', 20)])
    async with async_session() as session:
        third = await refresh_split_suggestions(session)
        await session.commit()
    assert third > 0




@pytest.mark.asyncio
async def test_apply_creates_children_moves_rules_and_reclassifies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """適用で子が作られ、ルールが移り、記事が再分類される。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Article, Genre, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    # ai + 未ルールタグ agent の記事を多く作り、promote_free_tags が成立する状況にする
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if r.strategy in {"promote_free_tags", "demote_generic"})
        proposal = payload_to_proposal(target.payload)
        created, moved, reclassified = await apply_suggestion(session, target.id)
        await session.commit()

    async with async_session() as session:
        # demote_generic なら子は増えない。promote_free_tags なら子が増える
        if proposal.children:
            assert created == len(proposal.children)
            keys = {
                g.key for g in (await session.execute(select(Genre))).scalars().all()
            }
            assert {c.key for c in proposal.children} <= keys
        else:
            assert created == 0
            assert moved > 0
        # 適用したら記事の genre が実際に動いている
        assert reclassified > 0
        assert (await session.execute(select(Article))).scalars().first() is not None
        # 適用した行は閉じている
        applied = await session.get(GenreSplitSuggestion, target.id)
        assert applied is not None and applied.dismissed_at is not None


@pytest.mark.asyncio
async def test_apply_overrides_child_labels(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """承認時に編集したラベルが使われる。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if payload_to_proposal(r.payload).children)
        child_key = payload_to_proposal(target.payload).children[0].key
        await apply_suggestion(session, target.id, labels={child_key: "AIエージェント"})
        await session.commit()

    async with async_session() as session:
        genre = (
            await session.execute(select(Genre).where(Genre.key == child_key))
        ).scalar_one()
        assert genre.label_ja == "AIエージェント"


@pytest.mark.asyncio
async def test_apply_closes_the_other_pending_proposals_for_the_same_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 つ適用すると、同ジャンルの他の案も閉じる（projected_max が無効になるため）。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import apply_suggestion, refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai", "security"]', 30), ('["ai"]', 10)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        same_genre = [r for r in rows if r.genre_key == rows[0].genre_key]
        if len(same_genre) < 2:
            pytest.skip("この辞書では同ジャンルに複数案が立たなかった")
        await apply_suggestion(session, same_genre[0].id)
        await session.commit()

    async with async_session() as session:
        after = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        for row in after:
            if row.genre_key == same_genre[0].genre_key:
                assert row.dismissed_at is not None


@pytest.mark.asyncio
async def test_dismiss_suppresses_until_the_count_grows(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視した後は、未読がその時点より増えるまで再提案されない。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import (
        dismiss_suggestion,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()
    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        for row in rows:
            await dismiss_suggestion(session, row.id)
        await session.commit()

    # 件数が変わらないので再提案されない
    async with async_session() as session:
        assert await refresh_split_suggestions(session) == 0
        await session.commit()

    # 未読が増えたら再提案される
    await _make_articles([('["ai"]', 20)])
    async with async_session() as session:
        assert await refresh_split_suggestions(session) > 0
        await session.commit()


@pytest.mark.asyncio
async def test_apply_to_a_childless_top_level_genre_makes_children_of_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子を持たないトップレベルジャンルに適用すると、新しい子はそのジャンル自身の
    子になる（兄弟ではない）。

    上の `test_apply_creates_children_moves_rules_and_reclassifies` は
    seed-subgenres 後の "ai_misc"（すでに子）を使うので、新しい子が
    「兄弟」になる分岐だけを通る。このテストは "security"（子を持たない
    トップレベル）を使い、「新しい子はそのジャンル自身の子になる」という
    もう一方の分岐（このタスクの核となる階層規則）を別途固定する。
    seed-subgenres は呼ばない。
    """
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    # security (子を持たないトップレベル) を上限超にする。monitoring は
    # 未ルールタグ（promote_free_tags で新しい子になる）
    await _make_articles([('["security"]', 30), ('["security", "monitoring"]', 25)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if payload_to_proposal(r.payload).children)
        child_key = payload_to_proposal(target.payload).children[0].key
        await apply_suggestion(session, target.id)
        await session.commit()

    async with async_session() as session:
        security = (
            await session.execute(select(Genre).where(Genre.key == "security"))
        ).scalar_one()
        child = (
            await session.execute(select(Genre).where(Genre.key == child_key))
        ).scalar_one()
        # 兄弟ではなく security 自身の子になっている
        assert child.parent_id == security.id
        # 兄弟の場合と同じ規則で親の priority を継ぐ（この場合は親 = security 自身）
        assert child.priority == security.priority


@pytest.mark.asyncio
async def test_apply_from_other_creates_a_genuine_top_level_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """genre_key="other" からの適用は、新しいジャンルを本当のトップレベルにする
    (parent_id=None, priority=DEFAULT_NEW_GENRE_PRIORITY)。

    この is_other 分岐は今まで手元の検証スクリプトでしか確かめていなかった
    ので、テストとして固定する。
    """
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreSplitSuggestion
    from app.services.genre_split_planner import DEFAULT_NEW_GENRE_PRIORITY
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    # どちらのルールにも当たらないタグを 2 種類使う。1 種類だけだと
    # promote_free_tags が全件を 1 つの新ジャンルに詰め込むだけになり、
    # 詰め込んだ先も上限を超えて棄却される（分割の意味がない）
    await _make_articles([('["zzzalpha"]', 30), ('["zzzbeta"]', 25)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if r.genre_key == "other")
        proposal = payload_to_proposal(target.payload)
        assert proposal.children  # other は promote_free_tags でしか成立しない
        target_id = target.id

    async with async_session() as session:
        await apply_suggestion(session, target_id)
        await session.commit()

    async with async_session() as session:
        for child in proposal.children:
            genre = (
                await session.execute(select(Genre).where(Genre.key == child.key))
            ).scalar_one()
            # 本当にトップレベル: 親を持たず、default priority を使う
            assert genre.parent_id is None
            assert genre.priority == DEFAULT_NEW_GENRE_PRIORITY
        # 何かの子にもなっていない（他ジャンルの parent_id から見ても孤立している）
        all_genres = (await session.execute(select(Genre))).scalars().all()
        new_keys = {c.key for c in proposal.children}
        assert not any(g.parent_id is not None and g.key in new_keys for g in all_genres)


@pytest.mark.asyncio
async def test_apply_resets_is_generic_when_reassigning_an_existing_rule(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存の GenreRule を新しい子へ付け替えるとき、is_generic は False に戻る。

    _simulate の tag_moves は移した先を常に tag_to_genre（通常段）に置いて
    件数を測っている。付け替え後も is_generic=True が残っていると、通常段
    より優先度の低い汎用段に落ちてしまい、reclassify_all の結果が利用者の
    承認した件数と食い違う。ここでは「スナップショット保存後、利用者が
    管理画面でそのタグを is_generic=True に手動で変えた」状況を直接 DB
    操作で作って確認する。

    対比: genre_seed.seed_subgenres がタグを子へ移すときは is_generic を
    保つ（technology は汎用のまま子へ移る、というシード投入としては正しい
    作法）。ここは提案の想定（tag_moves は常に通常段）に合わせる別の話なので、
    明示的に False にする。
    """
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreRule, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    async with async_session() as session:
        widgets = Genre(key="widgets", label_ja="ウィジェット", priority=50)
        session.add(widgets)
        await session.flush()
        session.add(GenreRule(tag="widget-a", genre_id=widgets.id, is_generic=False))
        session.add(GenreRule(tag="widget-b", genre_id=widgets.id, is_generic=False))
        await session.commit()

    # widget-a が多数派（受け皿として残る）、widget-b が少数派（新しい兄弟へ移る）
    await _make_articles([('["widget-a"]', 40), ('["widget-b"]', 20)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(
            r for r in rows if r.strategy == "split_own_tags" and r.genre_key == "widgets"
        )
        proposal = payload_to_proposal(target.payload)
        assert proposal.children and proposal.children[0].tags == ("widget-b",)
        target_id = target.id
        child_key = proposal.children[0].key

    # スナップショット保存後、利用者が widget-b のルールを汎用に変えたとする
    async with async_session() as session:
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == "widget-b"))
        ).scalar_one()
        rule.is_generic = True
        await session.commit()

    async with async_session() as session:
        await apply_suggestion(session, target_id)
        await session.commit()

    async with async_session() as session:
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == "widget-b"))
        ).scalar_one()
        child = (
            await session.execute(select(Genre).where(Genre.key == child_key))
        ).scalar_one()
        assert rule.genre_id == child.id
        assert rule.is_generic is False


@pytest.mark.asyncio
async def test_apply_raises_when_a_child_key_collides_with_an_unrelated_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案の保存後に、子キーと同名の無関係なジャンルが独立に作られていたら、
    何も変更せず ValueError で失敗する（黙って乗っ取らない）。

    プランナーは計画時にキー衝突を弾くが、計画から適用までの間に利用者が
    そのキーのジャンルを別に作った/動かしたら防げない。この操作は利用者の
    辞書を書き換えるので、黙って誤って書き換えるより、はっきり失敗させて
    再提案（refresh）を促すほうがずっと良い。
    """
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreRule, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await _make_articles([('["security", "python"]', 40), ('["security", "monitoring"]', 20)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if payload_to_proposal(r.payload).children)
        child_key = payload_to_proposal(target.payload).children[0].key  # "security_monitoring"
        target_id = target.id

    # 提案の保存後に、同じキーの無関係なトップレベルジャンルが独立に作られたとする
    # （apply が意図する親は security の id。無関係なジャンルは parent_id=None
    # で食い違う）
    async with async_session() as session:
        unrelated = Genre(key=child_key, label_ja="無関係なジャンル", priority=999)
        session.add(unrelated)
        await session.commit()
        unrelated_id = unrelated.id

    async with async_session() as session:
        with pytest.raises(ValueError):
            await apply_suggestion(session, target_id)

    async with async_session() as session:
        # 何も変更されていない: 無関係なジャンルはそのまま
        unrelated_after = await session.get(Genre, unrelated_id)
        assert unrelated_after is not None
        assert unrelated_after.label_ja == "無関係なジャンル"
        assert unrelated_after.parent_id is None
        assert unrelated_after.priority == 999
        # monitoring タグにはまだルールが付いていない（変更フェーズに到達していない）
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == "monitoring"))
        ).scalar_one_or_none()
        assert rule is None
        # 提案自体もまだ保留中（閉じられていない）
        suggestion = await session.get(GenreSplitSuggestion, target_id)
        assert suggestion is not None and suggestion.dismissed_at is None


@pytest.mark.asyncio
async def test_fetch_all_feeds_refreshes_split_suggestions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フィード取得サイクルの末尾で提案が更新される。"""
    called: list[bool] = []

    from app.services import feed_fetcher, genre_split_store

    async def fake_refresh(session):
        called.append(True)
        return 0

    monkeypatch.setattr(genre_split_store, "refresh_split_suggestions", fake_refresh)
    await feed_fetcher.fetch_all_feeds()

    assert called == [True]


@pytest.mark.asyncio
async def test_fetch_all_feeds_survives_a_failing_refresh(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案生成が落ちてもフィード取得は失敗しない（取得の方が重要）。"""
    from app.services import feed_fetcher, genre_split_store

    async def boom(session):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(genre_split_store, "refresh_split_suggestions", boom)
    await feed_fetcher.fetch_all_feeds()  # 例外が漏れないこと
