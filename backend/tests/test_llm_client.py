"""Tests for llm_client.chat_completion のリクエスト payload 組み立て。"""

import pytest

from app.ai import llm_client, task_queue
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
