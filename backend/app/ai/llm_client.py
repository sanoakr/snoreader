"""OpenAI-compatible client for local LLM (mlx-lm.server)."""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 512,
    temperature: float = 0.3,
    priority: int | None = None,
) -> str | None:
    """Send a chat completion request through the priority queue.

    Returns the assistant message content, or None on failure.
    priority defaults to PRIORITY_BACKGROUND when omitted.
    """
    from app.ai.task_queue import PRIORITY_BACKGROUND, enqueue

    if priority is None:
        priority = PRIORITY_BACKGROUND

    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async def _call() -> str | None:
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                resp = await client.post(
                    f"{settings.llm_base_url}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            logger.debug("LLM server not available at %s", settings.llm_base_url)
            return None
        except Exception as e:
            logger.warning("LLM request failed: %s", e)
            return None

    return await enqueue(_call, priority)


async def is_available() -> bool:
    """Check if the LLM server is reachable (direct call, not queued)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.llm_base_url}/models")
            return resp.status_code == 200
    except Exception:
        return False
