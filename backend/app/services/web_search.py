"""Lightweight DuckDuckGo web search helper for the article chat endpoint."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TypedDict

logger = logging.getLogger(__name__)

_MAX_RESULTS = 3
_TIMEOUT_SEC = 8.0
_SNIPPET_MAX_CHARS = 400  # 1 件あたりの本文プレビュー上限

# トリガー語: 常時発火
_ALWAYS_TRIGGERS = (
    "検索", "調べて", "search",
)
# トリガー語: 常時発火 (時事性キーワード)
_RECENCY_TRIGGERS = (
    "最新", "latest",
)
# 「今...?」「今の...?」などの疑問文パターン
# 誤検知回避のため、語尾に ? / ？ があるときだけ発火する
_NOW_PATTERN = re.compile(
    r"(今何|今の|今は|今日|今週|今年|today|right now).{0,30}[?？]",
    re.IGNORECASE,
)


def needs_web_search(message: str) -> bool:
    """記事本文外の Web 検索が必要か判定する。"""
    if not message:
        return False
    lower = message.lower()
    if any(t in lower for t in _ALWAYS_TRIGGERS):
        return True
    if any(t in lower for t in _RECENCY_TRIGGERS):
        return True
    if _NOW_PATTERN.search(message):
        return True
    return False


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


def _search_sync(query: str) -> list[SearchResult]:
    """Synchronous ddgs call. Runs in a thread because ddgs is blocking."""
    from ddgs import DDGS

    results: list[SearchResult] = []
    try:
        for item in DDGS().text(query, max_results=_MAX_RESULTS):
            body = (item.get("body") or "")[:_SNIPPET_MAX_CHARS]
            results.append(
                SearchResult(
                    title=item.get("title") or "",
                    url=item.get("href") or "",
                    snippet=body,
                )
            )
    except Exception as e:  # ネットワーク失敗・レート制限など
        logger.warning("Web search failed for %r: %s", query, e)
    return results


async def search(query: str) -> list[SearchResult]:
    """Run DuckDuckGo text search in a worker thread with a hard timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query),
            timeout=_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search timed out after %.1fs: %r", _TIMEOUT_SEC, query)
        return []


def format_results_for_llm(results: list[SearchResult]) -> str:
    """LLM に注入しやすいプレーンテキスト形式で整形する。"""
    if not results:
        return "(No relevant web results found.)"
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']}\n    URL: {r['url']}\n    {r['snippet']}")
    return "\n\n".join(lines)
