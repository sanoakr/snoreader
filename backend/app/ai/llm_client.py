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


# これ以下の長さの行は周期の一部とみなさない。要約の "・" や区切り線など、
# 繰り返されて当然の短い行を巻き込まないための下限
_MIN_REPEATED_LINE_LENGTH = 8
# 検出する反復単位の最大行数。実測では 1 行の周期と 2 段落の周期の両方が出た
_MAX_CYCLE_LINES = 6
# 2 周までは正当な繰り返しがありうる。3 周以上で暴走とみなす
_RUNAWAY_CYCLE_REPEATS = 3


def _collapse_repeated_lines(content: str) -> str:
    """Cut a runaway repetition loop back to a single cycle.

    The model sometimes answers correctly and then repeats itself verbatim until
    it hits max_tokens (measured at 10001 chars, finish_reason="length"). Observed
    in two shapes: one sentence repeating, and a two-paragraph A/B/A/B cycle —
    hence detecting a cycle of any length rather than just a repeated line.
    ``frequency_penalty`` makes this rarer but cannot rule it out, so it is also
    handled here, where the behaviour is deterministic and testable.

    The loop always runs to the end of the message (that is what exhausts the token
    budget), so the repetition is always a suffix and cutting it is safe. Blank
    lines are ignored when matching, because the observed loops put one between
    each repeat.
    """
    lines = content.splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    body = [lines[i].strip() for i in filled]
    n = len(body)

    for cycle_len in range(1, _MAX_CYCLE_LINES + 1):
        if n < cycle_len * _RUNAWAY_CYCLE_REPEATS:
            break
        cycle = body[n - cycle_len :]
        if any(len(line) < _MIN_REPEATED_LINE_LENGTH for line in cycle):
            continue
        repeats = 1
        while (
            n - cycle_len * (repeats + 1) >= 0
            and body[n - cycle_len * (repeats + 1) : n - cycle_len * repeats] == cycle
        ):
            repeats += 1
        if repeats >= _RUNAWAY_CYCLE_REPEATS:
            # 1 周分だけ残し、それ以降を捨てる
            first_dropped = n - cycle_len * (repeats - 1)
            return "\n".join(lines[: filled[first_dropped]]).rstrip()
    return content


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 512,
    temperature: float = 0.3,
    priority: int | None = None,
    lane: str = "reserved",
    frequency_penalty: float | None = None,
) -> str | None:
    """Send a chat completion request through the priority queue.

    Returns the assistant message content, or None on failure.
    priority defaults to PRIORITY_BACKGROUND when omitted. lane defaults to
    "reserved" (shared by foreground calls and Phase 2); background_processor's
    Phase 1 explicitly passes lane="bulk" — see app/ai/task_queue.py.

    frequency_penalty is opt-in per call rather than global: summaries repeat "・"
    and tag output repeats "|" and "," by design, so penalising repeated tokens
    everywhere would damage the structured formats. Only chat passes it.
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
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty

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
            answer = _strip_thinking(content)
            return _collapse_repeated_lines(answer) if answer else None
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
