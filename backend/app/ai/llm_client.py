"""OpenAI-compatible client for local LLM (mlx-lm.server)."""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_THINK_CLOSE = "</think>"
_THINK_OPEN = "<think>"


def _strip_thinking(content: str) -> str | None:
    """Drop a thinking block the server left in the message body.

    ``reasoning_effort="none"`` tells Ollama not to parse thinking, so when the
    model thinks anyway (measured at roughly 1 reply in 4 for qwen3.8) the
    reasoning text and its closing tag land in ``content``. The opening tag is
    injected by the chat template rather than generated, so the observed shape is
    ``<draft>…</think><answer>`` with no ``<think>`` at all — hence splitting on
    the closing tag rather than matching a pair.

    Returns None when nothing usable is left (the reply was cut off mid-thinking).
    """
    if _THINK_CLOSE in content:
        # 最後の閉じタグを採る。思考が複数ブロックに分かれても回答だけが残る
        logger.debug("stripped a thinking block from the LLM response")
        content = content.rsplit(_THINK_CLOSE, 1)[1]
    elif _THINK_OPEN in content:
        # 開いたまま閉じていない = max_tokens 等で思考の途中で切れており回答がない
        return None
    stripped = content.strip()
    return stripped or None


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 512,
    temperature: float = 0.3,
    priority: int | None = None,
    lane: str = "reserved",
) -> str | None:
    """Send a chat completion request through the priority queue.

    Returns the assistant message content, or None on failure.
    priority defaults to PRIORITY_BACKGROUND when omitted. lane defaults to
    "reserved" (shared by foreground calls and Phase 2); background_processor's
    Phase 1 explicitly passes lane="bulk" — see app/ai/task_queue.py.
    """
    from app.ai.task_queue import PRIORITY_BACKGROUND, enqueue

    if priority is None:
        priority = PRIORITY_BACKGROUND

    payload: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # 空文字は「サーバが解釈できないので送らない」の意（app/config.py 参照）
    if settings.llm_reasoning_effort:
        payload["reasoning_effort"] = settings.llm_reasoning_effort

    async def _call() -> str | None:
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                resp = await client.post(
                    f"{settings.llm_base_url}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                return None
            return _strip_thinking(content)
        except httpx.ConnectError:
            logger.debug("LLM server not available at %s", settings.llm_base_url)
            return None
        except Exception as e:
            logger.warning("LLM request failed: %s", e)
            return None

    return await enqueue(_call, priority, lane)


async def is_available() -> bool:
    """Check if the LLM server is reachable (direct call, not queued)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.llm_base_url}/models")
            return resp.status_code == 200
    except Exception:
        return False
