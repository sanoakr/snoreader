"""記事チャットの「必ず Web 検索」（force_web_search）のテスト。

ニュースの質問は記事にも学習データにも答えがないので、質問の文面に頼った
トリガー判定（needs_web_search）では検索が走らないことがある。フラグが立って
いるときは判定を飛ばして必ず検索し、結果を主な根拠として使わせる。
LLM と検索はモックする（実サーバーに依存しない）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# needs_web_search が False を返す（記事の言い換えを求める）質問
NO_TRIGGER_MESSAGE = "この記事を要約して"
RESULT = {"title": "速報", "url": "https://news.example/1", "snippet": "本文"}


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

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
            session.add(
                Article(
                    feed_id=feed.id,
                    guid="a1",
                    url="https://example.com/1",
                    title="記事",
                    summary="本文",
                )
            )
            await session.commit()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await database_module.engine.dispose()


class _Spy:
    """検索と LLM の呼び出しを記録する。"""

    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results
        self.search_queries: list[str] = []
        self.system_prompts: list[str] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    s = _Spy([RESULT])

    async def _search(query: str):
        s.search_queries.append(query)
        return s.results

    async def _chat(messages, **kwargs):
        s.system_prompts.append(messages[0]["content"])
        return "回答です"

    monkeypatch.setattr("app.services.web_search.search", _search)
    monkeypatch.setattr("app.ai.llm_client.chat_completion", _chat)
    return s


@pytest.mark.asyncio
async def test_force_flag_searches_even_without_trigger_words(
    client: AsyncClient, spy: _Spy
) -> None:
    res = await client.post(
        "/api/articles/1/chat",
        json={"message": NO_TRIGGER_MESSAGE, "force_web_search": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["search_used"] is True
    assert body["sources"] == [{"title": "速報", "url": "https://news.example/1"}]
    # 指示語だけの質問でも当たるよう、強制時は記事タイトルを前置する（記事のタイトルは「記事」）
    assert spy.search_queries == [f"記事 {NO_TRIGGER_MESSAGE}"]
    # 強制時は検索結果を「補足」ではなく主な根拠として使わせる文面になる
    assert "primary evidence" in spy.system_prompts[0]


@pytest.mark.asyncio
async def test_without_flag_keeps_trigger_heuristic(
    client: AsyncClient, spy: _Spy
) -> None:
    """フラグなし（従来の呼び方）では判定どおり、トリガー語のない質問は検索しない。"""
    res = await client.post("/api/articles/1/chat", json={"message": NO_TRIGGER_MESSAGE})
    assert res.status_code == 200
    assert res.json()["search_used"] is False
    assert spy.search_queries == []


@pytest.mark.asyncio
async def test_without_flag_still_searches_on_trigger_words(
    client: AsyncClient, spy: _Spy
) -> None:
    res = await client.post(
        "/api/articles/1/chat", json={"message": "最新の状況を調べて"}
    )
    assert res.json()["search_used"] is True
    # 通常トリガー時はタイトルを混ぜない（従来どおり）
    assert spy.search_queries == ["最新の状況を調べて"]
    # 通常トリガー時の文面は従来どおり（強制用の文面に変わっていないこと）
    assert "primary evidence" not in spy.system_prompts[0]


@pytest.mark.asyncio
async def test_force_flag_with_no_results_still_answers(
    client: AsyncClient, spy: _Spy
) -> None:
    """検索が 0 件・失敗でも、記事と知識で答えて 200 を返す（従来のフォールバック）。"""
    spy.results = []
    res = await client.post(
        "/api/articles/1/chat",
        json={"message": NO_TRIGGER_MESSAGE, "force_web_search": True},
    )
    assert res.status_code == 200
    assert res.json()["search_used"] is False
    assert res.json()["sources"] == []
