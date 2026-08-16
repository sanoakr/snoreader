"""Tests for llm_client.chat_completion のリクエスト payload 組み立てと応答の後処理。"""

import pytest

from app.ai import llm_client, task_queue
from app.ai.llm_client import _collapse_repeated_lines, _strip_thinking
from app.config import settings


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": "ok"}}]}


def _fake_client_factory(captured: dict):
    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse()

    return _FakeClient


async def _call_capturing(monkeypatch) -> dict:
    """chat_completion を 1 回呼び、送信された payload を返す。"""
    captured: dict = {}
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _fake_client_factory(captured))
    task_queue.start()
    try:
        result = await llm_client.chat_completion([{"role": "user", "content": "hi"}])
    finally:
        await task_queue.stop()
    assert result == "ok"
    return captured["payload"]


@pytest.mark.asyncio
async def test_reasoning_effort_is_sent_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "none")
    payload = await _call_capturing(monkeypatch)
    assert payload["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_reasoning_effort_omitted_when_blank(monkeypatch):
    # 未対応サーバ向けに、空文字ならパラメータ自体を送らない
    monkeypatch.setattr(settings, "llm_reasoning_effort", "")
    payload = await _call_capturing(monkeypatch)
    assert "reasoning_effort" not in payload


@pytest.mark.asyncio
async def test_reasoning_effort_passthrough_of_other_values(monkeypatch):
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    payload = await _call_capturing(monkeypatch)
    assert payload["reasoning_effort"] == "low"


# --- thinking ブロックの除去 -------------------------------------------
#
# reasoning_effort="none" のとき Ollama は thinking を解析しないので、モデルが
# それでも思考すると本文と閉じタグが content に落ちてくる。開始タグはチャット
# テンプレート側で注入されるため、生成側には現れないのが実測された形。


def test_strip_thinking_removes_unopened_block():
    """実測された形: 下書き + </think> + 本回答。閉じタグ以前を捨てること。"""
    raw = "下書きの回答です。\n</think>\n\n本当の回答です。"
    assert _strip_thinking(raw) == "本当の回答です。"


def test_strip_thinking_removes_paired_block():
    raw = "<think>ここは思考</think>\n\n本当の回答です。"
    assert _strip_thinking(raw) == "本当の回答です。"


def test_strip_thinking_uses_last_close_tag():
    raw = "思考1</think>思考2</think>最終的な回答"
    assert _strip_thinking(raw) == "最終的な回答"


def test_strip_thinking_keeps_normal_text_untouched():
    raw = "ふつうの回答です。\n\n2 段落目。"
    assert _strip_thinking(raw) == raw


def test_strip_thinking_returns_none_for_unclosed_block():
    """max_tokens で思考の途中で切れた場合、回答部分が存在しない。"""
    assert _strip_thinking("<think>思考の途中で切れた") is None


def test_strip_thinking_returns_none_when_nothing_follows():
    assert _strip_thinking("思考だけ</think>   \n ") is None


@pytest.mark.asyncio
async def test_chat_completion_strips_thinking_from_response(monkeypatch):
    """後処理は llm_client の境界で行う（全呼び出し元が素通しになるため）。"""
    captured: dict = {}
    factory = _fake_client_factory(captured)

    class _ThinkingResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "下書き\n</think>\n\n本当の回答"}}]}

    class _ThinkingClient(factory):  # type: ignore[misc, valid-type]
        async def post(self, url, json):
            return _ThinkingResponse()

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _ThinkingClient)
    task_queue.start()
    try:
        result = await llm_client.chat_completion([{"role": "user", "content": "hi"}])
    finally:
        await task_queue.stop()
    assert result == "本当の回答"


# --- 反復ループの畳み込み -----------------------------------------------
#
# モデルが同じ一文を max_tokens まで繰り返すことがある（finish_reason=length、
# 実測で 10001 文字）。frequency_penalty で頻度は下げられるが確率的なので、
# 境界側で決定的に畳んでおく。


def test_collapse_repeated_lines_folds_runaway_loop():
    line = "I am unsure about the exact current pricing structure."
    raw = "正常な回答です。\n\n" + "\n".join([line] * 30)
    assert _collapse_repeated_lines(raw) == f"正常な回答です。\n\n{line}"


def test_collapse_repeated_lines_folds_loop_separated_by_blank_lines():
    """実測された形は反復文が空行で区切られていた。空行で run が切れないこと。"""
    line = "I am unsure about the exact current pricing structure."
    raw = "正常な回答です。\n\n" + "\n\n".join([line] * 30)
    assert _collapse_repeated_lines(raw) == f"正常な回答です。\n\n{line}"


def test_collapse_repeated_lines_keeps_two_in_a_row():
    """2 回までは意図的な繰り返しがありうるので残す。"""
    raw = "はい。\nはい。\n続きます。"
    assert _collapse_repeated_lines(raw) == raw


def test_collapse_repeated_lines_ignores_short_lines():
    """箇条書き記号や区切りだけの短い行は畳まない（要約の '・' などを壊さないため）。"""
    raw = "---\n---\n---\n---\n本文"
    assert _collapse_repeated_lines(raw) == raw


def test_collapse_repeated_lines_folds_multi_line_cycle():
    """実測された暴走は 2 段落の周期だった（単一行の反復だけでは取りこぼす）。"""
    a = "（※運用コストの説明は記事外からの知識に基づくものです。）"
    b = "記事では月額費用や従量課金の詳細については触れられていません。"
    raw = "正常な回答です。\n\n" + "\n\n".join([a, b] * 12)
    assert _collapse_repeated_lines(raw) == f"正常な回答です。\n\n{a}\n\n{b}"


def test_collapse_repeated_lines_keeps_a_cycle_repeated_twice():
    """2 周までは正当な繰り返しがありうるので残す。"""
    a = "これは最初の長めの文です。"
    b = "これは二番目の長めの文です。"
    raw = "\n\n".join([a, b, a, b])
    assert _collapse_repeated_lines(raw) == raw


def test_collapse_repeated_lines_keeps_blank_lines():
    raw = "一行目。\n\n\n\n二行目。"
    assert _collapse_repeated_lines(raw) == raw


def test_collapse_repeated_lines_leaves_normal_text_untouched():
    raw = "ふつうの回答です。\n\n2 段落目もふつうです。"
    assert _collapse_repeated_lines(raw) == raw


@pytest.mark.asyncio
async def test_chat_completion_collapses_repetition(monkeypatch):
    line = "I am unsure about the exact current pricing structure."
    factory = _fake_client_factory({})

    class _LoopingResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            body = "答えです。\n" + "\n".join([line] * 20)
            return {"choices": [{"message": {"content": body}}]}

    class _LoopingClient(factory):  # type: ignore[misc, valid-type]
        async def post(self, url, json):
            return _LoopingResponse()

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _LoopingClient)
    task_queue.start()
    try:
        result = await llm_client.chat_completion([{"role": "user", "content": "hi"}])
    finally:
        await task_queue.stop()
    assert result == f"答えです。\n{line}"


# --- frequency_penalty ---------------------------------------------------
#
# チャットだけに掛ける。要約の '・' やタグの '|' ',' は設計上繰り返されるので、
# 全呼び出しに掛けると構造化出力を壊す。


@pytest.mark.asyncio
async def test_frequency_penalty_omitted_by_default(monkeypatch):
    payload = await _call_capturing(monkeypatch)
    assert "frequency_penalty" not in payload


@pytest.mark.asyncio
async def test_frequency_penalty_sent_when_requested(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", _fake_client_factory(captured))
    task_queue.start()
    try:
        await llm_client.chat_completion(
            [{"role": "user", "content": "hi"}], frequency_penalty=0.5
        )
    finally:
        await task_queue.stop()
    assert captured["payload"]["frequency_penalty"] == 0.5
