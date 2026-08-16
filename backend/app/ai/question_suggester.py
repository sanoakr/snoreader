"""Suggested follow-up questions for the article chat panel.

A separate (small) LLM call rather than an extra section in ``processor``'s
combined prompt: the combined call runs for every article in the background,
while questions are only ever needed for articles the user actually opens the
chat on. Results are cached in ``Article.chat_suggestions`` so the call happens
at most once per article.
"""

from __future__ import annotations

import hashlib
import logging
import re

from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

# チップとして横に並べるので、件数も 1 件あたりの長さも抑える。
# MAX_QUESTION_LENGTH はプロンプトで指示する 20 字よりかなり緩い安全網で、
# 説明文を垂れ流したときだけ落とす（厳しくすると全滅して 503 になる）
MAX_QUESTIONS = 4
MAX_QUESTION_LENGTH = 40

# 会話履歴として送る直近のやり取り。これより古い往復は次の質問の材料にならず、
# プロンプトを膨らませて生成を遅くするだけなので落とす
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_MESSAGE_LENGTH = 400

_FORMAT_AND_RULES = (
    "Output format (follow EXACTLY — no extra text):\n"
    "・<question in Japanese>\n"
    "・<question in Japanese>\n\n"
    "Rules:\n"
    f"- Output {MAX_QUESTIONS} questions, one per line, each starting with '・'\n"
    "- Always write the questions in Japanese, even for an English article\n"
    "- Keep each question SHORT: 20 Japanese characters or fewer, ending with '？'\n"
)

_SYSTEM_PROMPT = (
    "You suggest follow-up questions a reader might ask about a news article.\n"
    f"{_FORMAT_AND_RULES}"
    "- Ask about this specific article: its background, its impact, unfamiliar "
    "terms it uses, or what happens next\n"
    "- Do not restate the title, and do not ask questions the article already answers\n"
    "- Return ONLY the bullet lines, nothing else"
)

_FOLLOWUP_SYSTEM_PROMPT = (
    "You suggest what a reader would naturally ask NEXT, given an article and the "
    "conversation they have had about it so far.\n"
    f"{_FORMAT_AND_RULES}"
    "- Build on the conversation: go deeper, ask about implications, or open an "
    "adjacent aspect of the article that has not come up yet\n"
    "- Never repeat a question already asked, and never ask something the last "
    "answer already covered\n"
    "- Return ONLY the bullet lines, nothing else"
)


def _format_history(history: list[dict[str, str]]) -> str:
    """Render the recent exchange as a compact transcript for the prompt."""
    lines: list[str] = []
    for message in history[-MAX_HISTORY_MESSAGES:]:
        role = "Reader" if message.get("role") == "user" else "Assistant"
        content = (message.get("content") or "").strip()[:MAX_HISTORY_MESSAGE_LENGTH]
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)

# 行頭の箇条書き記号・番号（「・」「-」「*」「1.」「1)」「Q1:」など）
_LEADER = re.compile(r"^(?:[・\-*•]|\(?\d+[.)]|[Qq]\d*[:：.])\s*")


def _parse_questions(raw: str) -> list[str]:
    """Parse LLM output into a de-duplicated list of question strings."""
    questions: list[str] = []
    for line in raw.splitlines():
        text = _LEADER.sub("", line.strip()).strip().strip("\"'")
        if not text or len(text) > MAX_QUESTION_LENGTH:
            continue
        if text in questions:
            continue
        questions.append(text)
        if len(questions) == MAX_QUESTIONS:
            break
    return questions


async def suggest_questions(
    title: str,
    text: str,
    priority: int | None = None,
    lane: str = "reserved",
) -> list[str]:
    """Generate follow-up question suggestions for an article.

    Returns an empty list when the LLM is unavailable or returns nothing usable.
    """
    # 記事ごとのハッシュ接頭辞で KV キャッシュの連鎖を切る（processor.py と同じ理由）
    uid = hashlib.md5(f"ask:{title}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Suggest questions only for this article.\n\n"
                f"Title: {title}\n\n{text[:2000]}"
            ),
        },
    ]
    result = await chat_completion(
        messages, max_tokens=512, temperature=0.4, priority=priority, lane=lane
    )
    if not result:
        return []
    return _parse_questions(result)


async def suggest_followup_questions(
    title: str,
    text: str,
    history: list[dict[str, str]],
    priority: int | None = None,
) -> list[str]:
    """Generate the next questions to ask, given the conversation so far.

    Conversation-dependent, so the caller must not cache the result on the article.
    Returns an empty list when the LLM is unavailable or returns nothing usable.
    """
    transcript = _format_history(history)
    # 会話が進むたびに接頭辞が変わるので、KV キャッシュの使い回しを避けられる
    uid = hashlib.md5(f"next:{title}:{transcript}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _FOLLOWUP_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Suggest the next questions for this conversation.\n\n"
                f"Title: {title}\n\n{text[:1500]}\n\n"
                f"Conversation so far:\n{transcript}"
            ),
        },
    ]
    result = await chat_completion(messages, max_tokens=512, temperature=0.4, priority=priority)
    if not result:
        return []
    return _parse_questions(result)
