"""記事チャットの質問候補（GET /api/articles/{id}/chat-suggestions）のテスト。

候補は LLM で生成して ``Article.chat_suggestions`` にキャッシュする。記事を開いた
だけで LLM を呼ばないこと（``generate=false`` が既定）が設計上の要点なので、
呼び出し有無まで含めて検証する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.ai.question_suggester import MAX_QUESTIONS, _parse_questions


# --- パーサ（純粋関数） ---------------------------------------------------


def test_parse_questions_strips_bullets_and_numbering():
    raw = "・この判決の影響は？\n- 背景を教えて\n1. 今後の見通しは？"
    assert _parse_questions(raw) == [
        "この判決の影響は？",
        "背景を教えて",
        "今後の見通しは？",
    ]


def test_parse_questions_caps_at_max():
    raw = "\n".join(f"・質問{i}は？" for i in range(MAX_QUESTIONS + 3))
    assert len(_parse_questions(raw)) == MAX_QUESTIONS


def test_parse_questions_drops_blank_and_duplicate_lines():
    raw = "・同じ質問は？\n\n・同じ質問は？\n・別の質問は？"
    assert _parse_questions(raw) == ["同じ質問は？", "別の質問は？"]


def test_parse_questions_drops_overlong_line():
    long_line = "あ" * 200
    raw = f"・{long_line}\n・短い質問は？"
    assert _parse_questions(raw) == ["短い質問は？"]


def test_parse_questions_returns_empty_for_garbage():
    assert _parse_questions("") == []


# --- エンドポイント -------------------------------------------------------


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # config / database / main は環境変数を読み込んだあとに import する必要がある
    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    from app.database import async_session
    from app.models import Article, Feed

    async with main_module.lifespan(main_module.app):
        async with async_session() as session:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
            session.add_all(
                [
                    Article(
                        feed_id=feed.id,
                        guid="a1",
                        url="https://example.com/1",
                        title="候補キャッシュ済みの記事",
                        summary="本文1",
                        ai_summary="・既存の要約",
                        chat_suggestions='["保存済みの質問は？"]',
                    ),
                    Article(
                        feed_id=feed.id,
                        guid="a2",
                        url="https://example.com/2",
                        title="候補未生成の記事",
                        summary="本文2",
                        ai_summary="・既存の要約",
                    ),
                ]
            )
            await session.commit()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await database_module.engine.dispose()


@pytest.mark.asyncio
async def test_cached_suggestions_returned_without_llm_call(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """キャッシュ済みなら LLM を呼ばずにそのまま返すこと。"""
    calls: list[str] = []

    async def _fail(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        calls.append("called")
        return None

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fail)

    res = await client.get("/api/articles/1/chat-suggestions")
    assert res.status_code == 200
    assert res.json() == {"questions": ["保存済みの質問は？"], "generated": False}
    assert calls == []


@pytest.mark.asyncio
async def test_missing_suggestions_returns_empty_without_generating(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate 未指定なら未生成記事でも LLM を呼ばず空で返すこと。"""
    calls: list[str] = []

    async def _fail(*args, **kwargs):  # pragma: no cover - 呼ばれたら失敗
        calls.append("called")
        return []

    monkeypatch.setattr("app.ai.question_suggester.suggest_questions", _fail)

    res = await client.get("/api/articles/2/chat-suggestions")
    assert res.status_code == 200
    assert res.json() == {"questions": [], "generated": False}
    assert calls == []


@pytest.mark.asyncio
async def test_generate_true_calls_llm_and_persists(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate=true で LLM を呼び、結果を chat_suggestions に保存すること。"""

    async def _fake_chat(messages, **kwargs):
        return "・この記事の要点は？\n・今後の影響は？"

    monkeypatch.setattr("app.ai.question_suggester.chat_completion", _fake_chat)

    res = await client.get("/api/articles/2/chat-suggestions?generate=true")
    assert res.status_code == 200
    assert res.json() == {
        "questions": ["この記事の要点は？", "今後の影響は？"],
        "generated": True,
    }

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        article = await session.get(Article, 2)
        assert article is not None
        assert article.chat_suggestions == '["この記事の要点は？", "今後の影響は？"]'


@pytest.mark.asyncio
async def test_generate_true_returns_503_when_llm_unavailable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM が応答しないときは 503 を返し、キャッシュを書かないこと。"""

    async def _no_llm(messages, **kwargs):
        return None

    monkeypatch.setattr("app.ai.question_suggester.chat_completion", _no_llm)

    res = await client.get("/api/articles/2/chat-suggestions?generate=true")
    assert res.status_code == 503

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        article = await session.get(Article, 2)
        assert article is not None
        assert article.chat_suggestions is None


@pytest.mark.asyncio
async def test_unknown_article_returns_404(client: AsyncClient) -> None:
    res = await client.get("/api/articles/999/chat-suggestions")
    assert res.status_code == 404
