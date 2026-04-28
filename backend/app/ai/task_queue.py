"""Priority queue for LLM calls — foreground (user) requests always run before background."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, TypeVar

logger = logging.getLogger(__name__)

PRIORITY_FOREGROUND = 0
PRIORITY_BACKGROUND = 10

T = TypeVar("T")


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    func: Callable[[], Coroutine[Any, Any, Any]] = field(compare=False)
    future: "asyncio.Future[Any]" = field(compare=False)


_queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
_seq = 0
_worker_task: asyncio.Task[None] | None = None


async def _worker() -> None:
    while True:
        item = await _queue.get()
        try:
            result = await item.func()
            if not item.future.done():
                item.future.set_result(result)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            _queue.task_done()


def start() -> None:
    global _worker_task
    _worker_task = asyncio.get_event_loop().create_task(_worker(), name="llm-worker")
    logger.info("LLM task queue worker started")


def stop() -> None:
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
    logger.info("LLM task queue worker stopped")


def queue_depth() -> int:
    return _queue.qsize()


async def enqueue(
    func: Callable[[], Coroutine[Any, Any, T]],
    priority: int = PRIORITY_BACKGROUND,
) -> T:
    """Submit an LLM call to the queue and await its result."""
    global _seq
    _seq += 1
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()
    await _queue.put(_Item(priority=priority, seq=_seq, func=func, future=future))
    return await future
