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


async def name_genres(
    tag_groups: list[tuple[str, ...]], *, priority: int | None = None
) -> list[str]:
    """各タグ集合に日本語ラベルを付ける。長さは必ず入力と同じ。

    priority は task_queue の優先度（省略時は PRIORITY_BACKGROUND）。この呼び出しは
    1 ワーカーの "reserved" レーンを使うので、スケジューラ発の呼び出しを前景優先度
    にすると、ユーザーのチャットや手動要約が最大 llm_timeout 秒待たされる。手動の
    再計算エンドポイントだけがユーザー操作として PRIORITY_FOREGROUND を渡す（#10）。
    """
    from app.ai.task_queue import PRIORITY_BACKGROUND

    if not tag_groups:
        return []

    effective_priority = PRIORITY_BACKGROUND if priority is None else priority
    fallback = [group[0] if group else "" for group in tag_groups]
    lines = ["，".join(group) for group in tag_groups]
    result = await chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        max_tokens=len(tag_groups) * _TOKENS_PER_GROUP + 256,
        temperature=0.1,
        priority=effective_priority,
        lane="reserved",
    )
    if not result:
        return fallback

    labels = [line.strip() for line in result.splitlines() if line.strip()]
    # 行数がずれた分はタグ名で埋める。提案の子の数と必ず一致させる
    return [labels[i] if i < len(labels) else fallback[i] for i in range(len(tag_groups))]
