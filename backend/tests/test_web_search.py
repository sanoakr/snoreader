"""Tests for web_search.needs_web_search のトリガー判定。"""

import pytest

from app.services.web_search import needs_web_search


@pytest.mark.parametrize("message", [
    "インド太平洋について検索して",
    "調べてほしいことがあります",
    "search for the original paper",
    "最新の状況は？",
    "latest numbers please",
    "今の株価は？",
])
def test_explicit_search_and_recency_trigger(message):
    assert needs_web_search(message) is True


@pytest.mark.parametrize("message", [
    "インド太平洋とは何ですか",
    "この言葉の意味は？",
    "なぜこうなったのですか",
    "背景を教えてください",
    "両者の違いを教えて",
    "what is FOIP?",
    "why did this happen",
])
def test_explanation_requests_trigger(message):
    # B: 「〜とは」のような自然な質問でも検索を走らせる
    assert needs_web_search(message) is True


@pytest.mark.parametrize("message", [
    "この記事の結論は何ですか",
    "本記事の背景を整理して",
    "記事の要約をお願いします",
    "本文にはなぜと書いてありますか",
])
def test_article_scoped_questions_do_not_trigger(message):
    # 記事自体への質問は、説明要求の語を含んでいても検索しない
    assert needs_web_search(message) is False


def test_explicit_search_beats_article_scope_hint():
    # 明示的な検索指示は記事スコープの語より優先される
    assert needs_web_search("この記事の背景を検索して補足して") is True


@pytest.mark.parametrize("message", ["", "ありがとう", "もう少し詳しく"])
def test_plain_messages_do_not_trigger(message):
    assert needs_web_search(message) is False
