"""Phase 3（チャット質問候補の背景生成）のテスト。

Phase 3 は要約済みの記事に対して質問候補を先回りで生成し、``Article.chat_suggestions``
に保存する。記事を開いたときにボタンを押さずチップが並ぶのはこの副作用による。
LLM は常にモックする（実サーバーに依存させない）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select


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

    yield

    await database_module.engine.dispose()


async def _seed(**article_kwargs) -> int:
    """記事を 1 件作って id を返す。"""
    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = (await session.execute(select(Feed))).scalars().first()
        if feed is None:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
        article_kwargs.setdefault("title", "記事タイトル")
        article_kwargs.setdefault("summary", "RSS 要約")
        article = Article(feed_id=feed.id, **article_kwargs)
        session.add(article)
        await session.commit()
        return article.id


@pytest.mark.asyncio
async def test_generates_and_persists_suggestions(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要約済み・候補未生成の記事に候補を生成して保存すること。"""
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", ai_summary="・要約")

    async def _fake_suggest(title, text, **kwargs):
        return ["この記事の要点は？", "今後の影響は？"]

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fake_suggest)

    assert await background_processor._process_phase3_one() is True

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        article = await session.get(Article, article_id)
        assert article is not None
        assert article.chat_suggestions == '["この記事の要点は？", "今後の影響は？"]'


@pytest.mark.asyncio
async def test_skips_articles_without_summary_or_already_suggested(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要約が無い記事・候補済みの記事は対象外（＝処理する記事が無い）。"""
    from app.services import background_processor

    await _seed(guid="a1", url="https://example.com/1")  # ai_summary なし
    await _seed(
        guid="a2",
        url="https://example.com/2",
        ai_summary="・要約",
        chat_suggestions='["既存の質問は？"]',
    )

    calls: list[str] = []

    async def _fail(title, text, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        calls.append(title)
        return []

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fail)

    assert await background_processor._process_phase3_one() is False
    assert calls == []


@pytest.mark.asyncio
async def test_unread_is_processed_before_read(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未読を既読より先に処理すること。

    未読フラグは保存フラグより優先する（他の Phase は保存済みを最優先にするが、
    チャットを開くのは主にこれから読む記事なので Phase 3 だけ順序が違う）。
    """
    from app.services import background_processor

    await _seed(
        guid="a1",
        url="https://example.com/1",
        ai_summary="・要約",
        is_read=True,
        is_saved=True,
    )
    unread_id = await _seed(
        guid="a2", url="https://example.com/2", ai_summary="・要約", is_read=False
    )

    titles: list[str] = []

    async def _fake_suggest(title, text, **kwargs):
        titles.append(title)
        return ["質問は？"]

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fake_suggest)

    assert await background_processor._process_phase3_one() is True

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        unread = await session.get(Article, unread_id)
        assert unread is not None
        assert unread.chat_suggestions is not None
    assert len(titles) == 1


@pytest.mark.asyncio
async def test_saved_is_processed_before_unsaved_unread(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未読どうしなら保存済みを先に処理すること。"""
    from app.services import background_processor

    await _seed(guid="a1", url="https://example.com/1", ai_summary="・要約")
    saved_id = await _seed(
        guid="a2", url="https://example.com/2", ai_summary="・要約", is_saved=True
    )

    async def _fake_suggest(title, text, **kwargs):
        return ["質問は？"]

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fake_suggest)

    assert await background_processor._process_phase3_one() is True

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        saved = await session.get(Article, saved_id)
        assert saved is not None
        assert saved.chat_suggestions is not None


@pytest.mark.asyncio
async def test_empty_result_is_not_persisted_and_backs_off(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM が候補を返さなければ保存せず、その記事を一定時間 skip すること。"""
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", ai_summary="・要約")

    async def _empty(title, text, **kwargs):
        return []

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _empty)

    assert await background_processor._process_phase3_one() is True

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        article = await session.get(Article, article_id)
        assert article is not None
        assert article.chat_suggestions is None

    assert article_id in background_processor._llm_skip_until
    # backoff 中は同じ記事を選び直さない
    assert await background_processor._process_phase3_one() is False


@pytest.mark.asyncio
async def test_llm_exception_backs_off_without_raising(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 呼び出しが例外を投げてもループを落とさず backoff すること。"""
    from app.services import background_processor

    article_id = await _seed(guid="a1", url="https://example.com/1", ai_summary="・要約")

    async def _boom(title, text, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _boom)

    assert await background_processor._process_phase3_one() is True
    assert article_id in background_processor._llm_skip_until


@pytest.mark.asyncio
async def test_runs_on_bulk_lane_at_idle_priority(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 の後ろに並ぶよう、bulk レーンの最低優先度で実行すること。"""
    from app.ai.task_queue import PRIORITY_IDLE
    from app.services import background_processor

    await _seed(guid="a1", url="https://example.com/1", ai_summary="・要約")

    captured: dict[str, object] = {}

    async def _fake_suggest(title, text, **kwargs):
        captured.update(kwargs)
        return ["質問は？"]

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fake_suggest)

    await background_processor._process_phase3_one()

    assert captured["priority"] == PRIORITY_IDLE
    assert captured["lane"] == "bulk"


@pytest.mark.asyncio
async def test_idle_priority_is_lower_than_background() -> None:
    """PRIORITY_IDLE は Phase 1 (PRIORITY_BACKGROUND) より後回しであること。"""
    from app.ai.task_queue import PRIORITY_BACKGROUND, PRIORITY_IDLE

    assert PRIORITY_IDLE > PRIORITY_BACKGROUND
