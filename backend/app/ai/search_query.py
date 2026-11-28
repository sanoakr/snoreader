"""Search-query generation for the article chat's forced web search.

The reader's message is often not searchable on its own ("それはいつから？"),
and prepending the article title (v0.16.1) still let DuckDuckGo drift to
generic pages. A small LLM call turns title + conversation + message into a
self-contained query. It runs only on the forced-search path, where the user
has already opted into the extra latency, and any failure falls back to the
plain ``"<title> <message>"`` query so a bad rewrite can never block a search.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)

# 直近の往復だけで指示語は解決できる。長い履歴はプロンプトを膨らませるだけ
MAX_HISTORY_MESSAGES = 4
MAX_HISTORY_MESSAGE_LENGTH = 300
# 検索エンジンに渡す長さの上限。説明文を垂れ流したときの安全網（プロンプトでは短く指示する）
MAX_QUERY_LENGTH = 100
# 出力は 1 行の検索語だけなので短くてよい
_QUERY_MAX_TOKENS = 64

_SYSTEM_PROMPT = (
    "You write ONE web search query for a news search engine.\n"
    "Input: a news article title, the recent conversation, and the reader's latest "
    "message. The message may be vague (\"since when?\", \"what happened next?\") — "
    "resolve pronouns and omitted subjects using the title and the conversation, so "
    "the query makes sense with no other context.\n"
    "Rules:\n"
    "- Output ONLY the query on a single line: no quotes, no explanation, no 'Query:' prefix\n"
    "- Use 3 to 8 specific keywords: names, organizations, products, places, the event\n"
    "- Keep the language of the article title (Japanese title -> Japanese query)\n"
    "- Add the aspect the reader is asking about (date, cause, reaction, numbers, ...)\n"
    "- Do not invent facts that are in neither the title nor the conversation"
)


def fallback_query(title: str, message: str) -> str:
    """LLM が使えないときの検索語。v0.16.1 までの挙動と同じ。"""
    return f"{title} {message}".strip()


def _clean_query(raw: str) -> str:
    """LLM の出力から検索語だけを取り出す。取れなければ空文字を返す。"""
    for line in raw.splitlines():
        text = line.strip()
        if text:
            break
    else:
        return ""
    # 「Query: 」「検索語：」のような前置きと、囲みの引用符・記号を落とす
    text = re.sub(r"^(search\s*)?(query|検索(クエリ|語|ワード)?)\s*[:：]\s*", "", text, flags=re.I)
    text = text.strip(" \t\"'`「」『』“”")
    return text[:MAX_QUERY_LENGTH].strip()


def _format_history(history: Sequence[object]) -> str:
    lines: list[str] = []
    for m in list(history)[-MAX_HISTORY_MESSAGES:]:
        role = "Reader" if getattr(m, "role", "") == "user" else "Assistant"
        content = str(getattr(m, "content", ""))[:MAX_HISTORY_MESSAGE_LENGTH]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def build_search_query(title: str, message: str, history: Sequence[object] = ()) -> str:
    """Return a self-contained search query; never raises, never returns empty."""
    # chat_completion は呼び出し時に import する（routers と同じ遅延 import の慣例。
    # テストが app.ai.llm_client.chat_completion を差し替えられるようにもなる）
    from app.ai.llm_client import chat_completion
    from app.ai.task_queue import PRIORITY_FOREGROUND

    fallback = fallback_query(title, message)
    conversation = _format_history(history)
    user_content = f"Article title: {title}\n\n"
    if conversation:
        user_content += f"Recent conversation:\n{conversation}\n\n"
    user_content += f"Reader's latest message: {message}\n\nSearch query:"
    try:
        raw = await chat_completion(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_tokens=_QUERY_MAX_TOKENS,
            temperature=0.0,
            priority=PRIORITY_FOREGROUND,
        )
    except Exception as e:  # 検索語の整形は補助なので、何があっても検索自体は続ける
        logger.warning("Search query generation failed: %s", e)
        return fallback
    if not raw:
        return fallback
    query = _clean_query(raw)
    return query or fallback
