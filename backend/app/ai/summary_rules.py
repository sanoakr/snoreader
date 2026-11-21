"""Shared rules and bullet-list post-processing for AI-generated article summaries."""

from __future__ import annotations

import re
from html.parser import HTMLParser

MIN_SUMMARY_BULLETS = 1
MAX_SUMMARY_BULLETS = 9

SUMMARY_RULES = (
    f"- SUMMARY: {MIN_SUMMARY_BULLETS}-{MAX_SUMMARY_BULLETS} Japanese bullet points — "
    "use as FEW as possible, only add a bullet if it conveys genuinely new information\n"
    "- Do NOT restate information already given in the title — every bullet must add "
    "something the title doesn't already say\n"
    "- Always include the article's conclusion, result, or outcome, so the title and "
    "summary together give a complete understanding of the article\n"
    "- SUMMARY bullets start with '・' and contain ONLY Japanese text — no English|Japanese pairs\n"
    "- Focus on key facts and takeaways only. Do not add opinions."
)

# Rejects summary bullets containing tag-format annotations like "security|セキュリティ"
_TAG_IN_BULLET = re.compile(r"\b[a-z]{2,}\|")


def finalize_bullets(lines: list[str]) -> str | None:
    """Drop tag-annotated lines, cap at MAX_SUMMARY_BULLETS, and join.

    `lines` must already be stripped, ・-prefixed bullet lines. Returns None
    if no valid bullet lines remain (treated as an LLM formatting failure).
    """
    cleaned = [line for line in lines if not _TAG_IN_BULLET.search(line)]
    cleaned = cleaned[:MAX_SUMMARY_BULLETS]
    return "\n".join(cleaned) if cleaned else None


# 要約・タグ生成に渡す本文の上限（字）。以前は 3,000 字で切っており、後半にある結論が落ちていた。
# lethe の context 32,768 トークンに出力とシステムプロンプトを足しても収まる値。
SUMMARY_INPUT_CHARS = 12_000
CLIP_SEPARATOR = "\n[…中略…]\n"
_CLIP_HEAD_RATIO_NUM = 2  # 先頭に 2/3、末尾に 1/3 を残す
_CLIP_HEAD_RATIO_DEN = 3


def clip_for_llm(text: str, limit: int) -> str:
    """LLM に渡す本文を limit 字に収める。超えたら先頭 2/3 と末尾 1/3 を残して中間を省く。

    結論は末尾にあることが多いので、単純な先頭切り詰めはしない。
    """
    if len(text) <= limit:
        return text
    head = limit * _CLIP_HEAD_RATIO_NUM // _CLIP_HEAD_RATIO_DEN
    tail = limit // 3
    return text[:head] + CLIP_SEPARATOR + text[-tail:]


# 改行を入れる（= 段落の区切りになる）ブロック要素
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "pre",
     "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "figure", "hr"}
)
_SKIP_TAGS = frozenset({"script", "style"})  # 中身ごと捨てる


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """抽出済み本文の HTML を文章にする。タグと属性は捨て、ブロック要素の境目を改行にする。

    HTML のまま LLM に渡すと、属性や img タグがトークンを食って、同じ字数でも
    文章の量が大きく減る。プレーンテキストを渡しても中身は変わらない。
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def prepare_for_llm(text: str, limit: int) -> str:
    """LLM に渡す本文を作る。HTML を文章にしてから limit 字に収める。"""
    return clip_for_llm(html_to_text(text), limit)
