"""Phase 4（Saved 記事の参照用要約を背景生成）のテスト。LLM は常にモックする。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, update

REFERENCE_TEXT = "【要旨】a\n【事実】b\n【主張と出どころ】c\n【時点】d"


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """テーブルだけ作る。lifespan は使わない（背景ループが同時に走ると干渉するため）。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app.models import Base

    async with database_module.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.services import background_processor

    background_processor._llm_skip_until.clear()
    background_processor._reference_skip_until.clear()

    yield

    await database_module.engine.dispose()


async def _seed(**article_kwargs) -> int:
    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = (await session.execute(select(Feed))).scalars().first()
        if feed is None:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
        article_kwargs.setdefault("title", "記事タイトル")
        article_kwargs.setdefault("summary", "RSS 抜粋")
        article_kwargs.setdefault("is_saved", True)
        article_kwargs.setdefault("saved_at", "2026-11-01T00:00:00+00:00")
        article = Article(feed_id=feed.id, **article_kwargs)
        session.add(article)
        await session.commit()
        return article.id


async def _get(article_id: int):
    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        return await session.get(Article, article_id)


def _mock_generator(monkeypatch: pytest.MonkeyPatch, *, result=REFERENCE_TEXT, side_effect=None):
    """generate_reference_summary を差し替え、渡された (title, text) を記録して返す。"""
    calls: list[tuple[str, str]] = []

    async def _fake(title, text, published_at, **kwargs):
        calls.append((title, text))
        if side_effect is not None:
            await side_effect()
        return result

    monkeypatch.setattr("app.ai.reference_summarizer.generate_reference_summary", _fake)
    return calls


@pytest.mark.asyncio
async def test_phase4_writes_all_four_columns(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.reference_summarizer import reference_version
    from app.config import settings
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", content="<p>本文</p>")
    calls = _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is True

    article = await _get(article_id)
    assert article.reference_summary == REFERENCE_TEXT
    assert article.reference_basis == "content"
    assert article.reference_summary_version == reference_version(settings.llm_model)
    assert article.reference_summary_at and "T" in article.reference_summary_at
    assert calls[0][1] == "<p>本文</p>"


@pytest.mark.asyncio
async def test_phase4_skips_unsaved_articles(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import background_processor

    await _seed(guid="a1", url="https://example.com/1", content="本文", is_saved=False)
    calls = _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is False
    assert calls == []


@pytest.mark.asyncio
async def test_phase4_newest_saved_first(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import background_processor

    old_id = await _seed(
        guid="old", url="https://example.com/old", content="古い", saved_at="2026-01-01T00:00:00+00:00"
    )
    new_id = await _seed(
        guid="new", url="https://example.com/new", content="新しい", saved_at="2026-11-01T00:00:00+00:00"
    )
    _mock_generator(monkeypatch)

    await background_processor._process_phase4_one()

    assert (await _get(new_id)).reference_summary is not None
    assert (await _get(old_id)).reference_summary is None


@pytest.mark.asyncio
async def test_phase4_uses_excerpt_when_extraction_failed(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import background_processor

    article_id = await _seed(
        guid="a1", url="https://example.com/1", content=None, extract_status="not_found", summary="抜粋です"
    )
    calls = _mock_generator(monkeypatch)

    await background_processor._process_phase4_one()

    assert (await _get(article_id)).reference_basis == "excerpt"
    assert calls[0][1] == "抜粋です"


@pytest.mark.asyncio
async def test_phase4_redoes_excerpt_once_content_is_available(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai.reference_summarizer import reference_version
    from app.config import settings
    from app.services import background_processor

    article_id = await _seed(
        guid="a1",
        url="https://example.com/1",
        content="<p>あとから取れた本文</p>",
        extract_status=None,
        reference_summary="古い抜粋由来の要約",
        reference_basis="excerpt",
        reference_summary_version=reference_version(settings.llm_model),
        reference_summary_at="2026-11-01T00:00:00+00:00",
    )
    calls = _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is True

    article = await _get(article_id)
    assert article.reference_basis == "content"
    assert article.reference_summary == REFERENCE_TEXT
    assert calls[0][1] == "<p>あとから取れた本文</p>"


@pytest.mark.asyncio
async def test_phase4_regenerates_only_older_prompt_version(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import background_processor

    stale_id = await _seed(
        guid="stale",
        url="https://example.com/stale",
        content="本文",
        reference_summary="古い版",
        reference_basis="content",
        reference_summary_version="m/ref-v0",
        reference_summary_at="2026-11-01T00:00:00+00:00",
    )
    _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is True
    assert (await _get(stale_id)).reference_summary == REFERENCE_TEXT


@pytest.mark.asyncio
async def test_phase4_ignores_model_name_difference(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai.reference_summarizer import current_version_suffix
    from app.services import background_processor

    await _seed(
        guid="a1",
        url="https://example.com/1",
        content="本文",
        reference_summary="別モデルで作った要約",
        reference_basis="content",
        reference_summary_version="other-model" + current_version_suffix(),
        reference_summary_at="2026-11-01T00:00:00+00:00",
    )
    calls = _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is False
    assert calls == []


@pytest.mark.asyncio
async def test_phase4_ignores_whitespace_only_input(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import background_processor

    await _seed(guid="a1", url="https://example.com/1", content="   ", summary="  ")
    calls = _mock_generator(monkeypatch)

    assert await background_processor._process_phase4_one() is False
    assert calls == []


@pytest.mark.asyncio
async def test_phase4_backs_off_on_none_without_touching_llm_skip(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", content="本文")
    _mock_generator(monkeypatch, result=None)

    assert await background_processor._process_phase4_one() is True

    assert (await _get(article_id)).reference_summary is None
    assert article_id in background_processor._reference_skip_until
    assert background_processor._llm_skip_until == {}
    # バックオフ中は同じ記事を選び直さない
    assert await background_processor._process_phase4_one() is False


@pytest.mark.asyncio
async def test_phase4_backs_off_on_exception(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", content="本文")

    async def _boom() -> None:
        raise RuntimeError("LLM down")

    _mock_generator(monkeypatch, side_effect=_boom)

    assert await background_processor._process_phase4_one() is True

    assert (await _get(article_id)).reference_summary is None
    assert article_id in background_processor._reference_skip_until
    assert background_processor._llm_skip_until == {}


@pytest.mark.asyncio
async def test_phase4_does_not_write_if_unsaved_meanwhile(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", content="本文")

    async def _unsave() -> None:
        async with async_session() as session:
            await session.execute(
                update(Article).where(Article.id == article_id).values(is_saved=False)
            )
            await session.commit()

    _mock_generator(monkeypatch, side_effect=_unsave)

    assert await background_processor._process_phase4_one() is True
    assert (await _get(article_id)).reference_summary is None


@pytest.mark.asyncio
async def test_phase4_handles_article_deleted_meanwhile(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", content="本文")

    async def _delete() -> None:
        async with async_session() as session:
            article = await session.get(Article, article_id)
            await session.delete(article)
            await session.commit()

    _mock_generator(monkeypatch, side_effect=_delete)

    assert await background_processor._process_phase4_one() is True
    assert await _get(article_id) is None
