"""同一ニュースの別媒体版クラスタリングのテスト。

判定は決定的な関数のみ（LLM は使わない）。真陽性・偽陽性の実例は
2026-09-10 に本番 DB 全 17,218 件を総当たりして選んだもの。
"""

from __future__ import annotations

import pytest

from app.services.story_clusterer import (
    SIMILARITY_THRESHOLD,
    is_ugc_host,
    title_similarity,
)


# --- 真陽性: 同じニュースを別媒体が報じたもの ---

@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            "キオクシア 上場来高値から半値に",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
        ),
        ("芸術家の草間彌生さん死去", "【速報】前衛芸術家の草間弥生さん死去"),
        (
            "ニチレイ障害 ハッカー集団が声明",
            "【速報】ニチレイ障害、ハッカー集団が犯行声明",
        ),
        (
            "VSCodeのCopilotがAzureの料金から開発まで何でも教えてくれる",
            "VSCodeのCopilotがAzureの料金から開発まで何でも教えてくれる",
        ),
    ],
)
def test_similarity_reaches_threshold_for_same_story(a: str, b: str) -> None:
    assert title_similarity(a, b) >= SIMILARITY_THRESHOLD


# --- 偽陽性: 閾値では分離できないので、UGC ホスト除外側で落とす ---

@pytest.mark.parametrize(
    "url",
    [
        "https://zenn.dev/u/articles/abc",
        "https://qiita.com/u/items/abc",
        "https://github.com/o/r",
        "https://note.com/u/n/abc",
        "https://example.hatenablog.com/entry/1",
        "https://speakerdeck.com/u/deck",
        "https://anond.hatelabo.jp/20260101000000",
        "https://togetter.com/li/1",
        "https://huggingface.co/org/model",
        "https://www.youtube.com/watch?v=abc",
        "https://hamusoku.com/archives/1.html",
        "https://blog.example.com/entry/1",
        "https://tech.example.co.jp/entry/1",
    ],
)
def test_ugc_hosts_are_excluded(url: str) -> None:
    assert is_ugc_host(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.itmedia.co.jp/news/articles/1.html",
        "https://gigazine.net/news/20260101-x/",
        "https://news.yahoo.co.jp/articles/abc",
        "https://www.publickey1.jp/blog/26/x.html",
        "https://www.techno-edge.net/article/2026/01/1.html",
    ],
)
def test_news_hosts_are_not_excluded(url: str) -> None:
    assert is_ugc_host(url) is False


def test_publickey_path_containing_blog_is_not_ugc():
    """`blog.` はサブドメイン接頭辞の意図。パス中の /blog/ で誤判定しない。"""
    assert is_ugc_host("https://www.publickey1.jp/blog/26/mcp.html") is False


def test_similarity_ignores_punctuation_and_spacing():
    assert title_similarity("桐谷広人さん　前立腺と大腸にがん", "桐谷広人さん 前立腺と大腸にがん") == 1.0


def test_similarity_is_zero_for_unrelated_titles():
    assert title_similarity("Pixel 11 シリーズ発表", "大腸がんと腸内細菌") < SIMILARITY_THRESHOLD


def test_similarity_handles_empty_titles():
    assert title_similarity("", "") == 0.0
    assert title_similarity("", "何か") == 0.0
