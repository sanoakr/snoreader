"""kioku が検索で引くための参照用要約（reference_summary）の生成と検証。

人が一覧で読む ai_summary とは目的が違う。固有名詞・版・型番・数値・日付を原文の表記のまま
入れ、語彙検索と意味検索の両方で記事単位に当たるようにする。combined call（要約+タグ）には
相乗りさせない: 出力形式が違い、タグ品質（ジャンル分類）を巻き込みたくないため。
"""

from __future__ import annotations

import hashlib

from app.ai import summary_rules
from app.ai.llm_client import chat_completion

# プロンプトを変えたら上げる。上げると古い版の要約が作り直し対象になる（モデル名の違いでは作り直さない）
REFERENCE_PROMPT_VERSION = 1
# 参照用要約に渡す本文の上限（字）。context 32,768 トークンに出力を足しても収まる
REFERENCE_INPUT_CHARS = 16_000
REFERENCE_MAX_TOKENS = 1536
# 検証: 狙いは 600〜1,000 字（最大 1,200 字）。生成のぶれを見込んで広めに許す
REFERENCE_MIN_CHARS = 300
REFERENCE_MAX_CHARS = 1500
REFERENCE_HEADINGS = ("【要旨】", "【事実】", "【主張と出どころ】", "【時点】")

_SYSTEM_PROMPT = (
    "あなたは記事の参照用要約を作る。読むための要約ではなく、あとで検索して引くための要約である。\n"
    "次の 4 つの見出しをこの順に、見出し行をそのまま使って出力する。前置きや後書きは書かない。\n"
    "【要旨】何についての記事で、結論は何か。1〜2 文。\n"
    "【事実】箇条書き（行頭は「・」）で 5〜10 個。固有名詞（製品名・組織名・人名）、版・型番、数値、"
    "日付を原文の表記のまま入れる。英語の名前は英語のまま書く。\n"
    "【主張と出どころ】誰が何を言っているか。「記事の主張」「公式の発表」「推測」を区別して書く。\n"
    "【時点】いつ時点の情報か。公開日、または「〜の版で」の形で書く。\n"
    "全体で 600〜1,000 字（長くても 1,200 字）にする。本文にないことは書かない。"
)


def reference_version(model: str) -> str:
    """生成器の版。モデル名とプロンプトの版から作る（例: qwen3.8:27b/ref-v1）。"""
    return f"{model}{current_version_suffix()}"


def current_version_suffix() -> str:
    """現行のプロンプト版を表す末尾（古い版の判定に使う）。"""
    return f"/ref-v{REFERENCE_PROMPT_VERSION}"


def validate_reference_summary(raw: str | None) -> str | None:
    """4 見出しが揃い、長さが範囲内で、思考の混入がなければ整形して返す。それ以外は None。"""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # reasoning_effort=none だと Ollama の thinking 解析も止まり、思考が本文に落ちることがある
    if "<think>" in text or "</think>" in text:
        return None
    if not all(heading in text for heading in REFERENCE_HEADINGS):
        return None
    if not REFERENCE_MIN_CHARS <= len(text) <= REFERENCE_MAX_CHARS:
        return None
    return text


async def generate_reference_summary(
    title: str,
    text: str,
    published_at: str | None,
    *,
    priority: int | None = None,
    lane: str = "reserved",
) -> str | None:
    """参照用要約を 1 件作る。LLM が使えない・出力が検証に通らないときは None。"""
    # 記事ごとのハッシュ前置きで、連続する記事の KV キャッシュ連鎖を切る（processor.py と同じ理由）
    uid = hashlib.md5(f"ref:{title}".encode()).hexdigest()[:8]
    body = summary_rules.prepare_for_llm(text, REFERENCE_INPUT_CHARS)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] この記事だけを要約する。\n\n"
                f"Title: {title}\nPublished: {published_at or 'unknown'}\n\n{body}"
            ),
        },
    ]
    raw = await chat_completion(
        messages,
        max_tokens=REFERENCE_MAX_TOKENS,
        temperature=0.2,
        priority=priority,
        lane=lane,
    )
    return validate_reference_summary(raw)
