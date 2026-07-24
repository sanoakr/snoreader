"""Priority queue for LLM calls — foreground (user) requests always run before background.

Two independent lanes run concurrently against the LLM backend (slab-llm's Ollama
was measured 2026-07-25 to handle ~2 concurrent chat completions at ~2x the
throughput of one at a time — see git history for the benchmark):

- "bulk": background_processor's Phase 1 (combined summarize+tag) calls only.
  2 workers, so up to 2 articles are summarized concurrently.
- "reserved": everything else — foreground (user-triggered) calls and Phase 2
  (tag-only backfill). 1 worker, so foreground requests always have a lane
  free instead of queuing behind the 2 bulk workers; Phase 2 uses this lane
  opportunistically whenever no foreground request needs it (priority
  ordering within the lane decides which runs first).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Literal, TypeVar

logger = logging.getLogger(__name__)

PRIORITY_FOREGROUND = 0
PRIORITY_BACKGROUND = 10

Lane = Literal["bulk", "reserved"]
_LANE_WORKERS: dict[Lane, int] = {"bulk": 2, "reserved": 1}

T = TypeVar("T")


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    func: Callable[[], Coroutine[Any, Any, Any]] = field(compare=False)
    future: "asyncio.Future[Any]" = field(compare=False)


_queues: dict[Lane, "asyncio.PriorityQueue[_Item]"] = {
    "bulk": asyncio.PriorityQueue(),
    "reserved": asyncio.PriorityQueue(),
}
_seq = 0
_worker_tasks: list[asyncio.Task[None]] = []


async def _worker(lane: Lane) -> None:
    queue = _queues[lane]
    while True:
        item = await queue.get()
        try:
            result = await item.func()
            if not item.future.done():
                item.future.set_result(result)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            queue.task_done()


def start() -> None:
    global _worker_tasks
    loop = asyncio.get_event_loop()
    _worker_tasks = [
        loop.create_task(_worker(lane), name=f"llm-worker-{lane}-{i}")
        for lane, count in _LANE_WORKERS.items()
        for i in range(count)
    ]
    logger.info("LLM task queue workers started: %s", _LANE_WORKERS)


def stop() -> None:
    for task in _worker_tasks:
        if not task.done():
            task.cancel()
    logger.info("LLM task queue workers stopped")


def queue_depth() -> int:
    return sum(q.qsize() for q in _queues.values())


async def enqueue(
    func: Callable[[], Coroutine[Any, Any, T]],
    priority: int = PRIORITY_BACKGROUND,
    lane: Lane = "reserved",
) -> T:
    """Submit an LLM call to the given lane's queue and await its result."""
    global _seq
    _seq += 1
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()
    await _queues[lane].put(_Item(priority=priority, seq=_seq, func=func, future=future))
    return await future
