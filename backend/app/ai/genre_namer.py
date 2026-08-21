"""タグ集合からジャンルの日本語ラベルを付ける。

分類そのものは辞書のみで行い LLM に依存しない（app/services/genre_classifier.py）。
LLM を使うのは提案作成時のラベル命名 1 回だけで、失敗してもタグ名に
フォールバックするので提案の生成は止まらない。
"""

from __future__ import annotations

from app.ai.llm_client import chat_completion

_SYSTEM = (
    "You name RSS article genres in Japanese. For each input line of comma-separated "
    "English tags, output ONE short Japanese genre label (at most 12 characters). "
    "Output exactly one label per input line, in the same order. "
    "No numbering, no quotes, no explanation."
)

# 1 ラベルあたりの余裕を見た上限（日本語 12 文字 + 改行）
_TOKENS_PER_GROUP = 24


async def name_genres(tag_groups: list[tuple[str, ...]]) -> list[str]:
    """各タグ集合に日本語ラベルを付ける。長さは必ず入力と同じ。"""
    from app.ai.task_queue import PRIORITY_FOREGROUND

    if not tag_groups:
        return []

    fallback = [group[0] if group else "" for group in tag_groups]
    lines = ["，".join(group) for group in tag_groups]
    result = await chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        max_tokens=len(tag_groups) * _TOKENS_PER_GROUP + 256,
        temperature=0.1,
        priority=PRIORITY_FOREGROUND,
        lane="reserved",
    )
    if not result:
        return fallback

    labels = [line.strip() for line in result.splitlines() if line.strip()]
    # 行数がずれた分はタグ名で埋める。提案の子の数と必ず一致させる
    return [labels[i] if i < len(labels) else fallback[i] for i in range(len(tag_groups))]
