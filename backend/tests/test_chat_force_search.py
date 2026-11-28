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
        self.built_from: list[tuple[str, str, int]] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    s = _Spy([RESULT])

    async def _search(query: str):
        s.search_queries.append(query)
        return s.results

    async def _chat(messages, **kwargs):
        s.system_prompts.append(messages[0]["content"])
        return "回答です"

    async def _build_query(title, message, history=()):
        s.built_from.append((title, message, len(history)))
        return "生成された検索語"

    monkeypatch.setattr("app.services.web_search.search", _search)
    monkeypatch.setattr("app.ai.search_query.build_search_query", _build_query)
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
    # 強制時は LLM が作った検索語で検索する（材料は記事タイトルと質問と履歴）
    assert spy.search_queries == ["生成された検索語"]
    assert spy.built_from == [("記事", NO_TRIGGER_MESSAGE, 0)]
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
    # 通常トリガー時は質問文のまま検索し、検索語の生成は呼ばない（従来どおり）
    assert spy.search_queries == ["最新の状況を調べて"]
    assert spy.built_from == []
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


# --- build_search_query（検索語の生成） ---------------------------------------


class _Msg:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def _patch_llm(monkeypatch: pytest.MonkeyPatch, reply, captured: list | None = None) -> None:
    async def _chat(messages, **kwargs):
        if captured is not None:
            captured.append((messages, kwargs))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("app.ai.llm_client.chat_completion", _chat)


@pytest.mark.asyncio
async def test_build_query_uses_llm_output_and_gives_it_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.search_query import build_search_query

    captured: list = []
    _patch_llm(monkeypatch, "Counterpoint 低価格スマホ 出荷 2030年", captured)
    history = [_Msg("user", "どこの調査？"), _Msg("assistant", "Counterpointです")]

    query = await build_search_query("安いスマホが消える", "それはいつから？", history)

    assert query == "Counterpoint 低価格スマホ 出荷 2030年"
    user_prompt = captured[0][0][1]["content"]
    # 指示語を解決する材料が全部プロンプトに入っている
    assert "安いスマホが消える" in user_prompt
    assert "Counterpointです" in user_prompt
    assert "それはいつから？" in user_prompt
    assert captured[0][1]["temperature"] == 0.0


@pytest.mark.parametrize("raw,expected", [
    ('"低価格スマホ 出荷"', "低価格スマホ 出荷"),
    ("検索クエリ：低価格スマホ 出荷", "低価格スマホ 出荷"),
    ("Query: low-end phone shipments", "low-end phone shipments"),
    ("\n低価格スマホ 出荷\n補足説明です", "低価格スマホ 出荷"),  # 2 行目以降は捨てる
])
@pytest.mark.asyncio
async def test_build_query_cleans_llm_output(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    from app.ai.search_query import build_search_query

    _patch_llm(monkeypatch, raw)
    assert await build_search_query("題", "質問") == expected


@pytest.mark.asyncio
async def test_build_query_caps_overlong_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.search_query import MAX_QUERY_LENGTH, build_search_query

    _patch_llm(monkeypatch, "あ" * 500)
    assert len(await build_search_query("題", "質問")) == MAX_QUERY_LENGTH


@pytest.mark.parametrize("reply", [None, "", "   \n", RuntimeError("llm down")])
@pytest.mark.asyncio
async def test_build_query_falls_back_to_title_and_message(
    monkeypatch: pytest.MonkeyPatch, reply
) -> None:
    """LLM 失敗・空出力でも検索は止めない（v0.16.1 の「タイトル + 質問」に戻る）。"""
    from app.ai.search_query import build_search_query

    _patch_llm(monkeypatch, reply)
    assert await build_search_query("記事題", "それはいつから？") == "記事題 それはいつから？"


@pytest.mark.asyncio
async def test_build_query_only_sends_recent_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.search_query import MAX_HISTORY_MESSAGES, build_search_query

    captured: list = []
    _patch_llm(monkeypatch, "検索語", captured)
    history = [_Msg("user", f"古い質問{i}") for i in range(MAX_HISTORY_MESSAGES + 3)]

    await build_search_query("題", "質問", history)

    user_prompt = captured[0][0][1]["content"]
    assert "古い質問0" not in user_prompt
    assert f"古い質問{MAX_HISTORY_MESSAGES + 2}" in user_prompt
