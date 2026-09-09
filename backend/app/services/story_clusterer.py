"""同じニュースを別媒体が報じた記事を検出し、代表 1 件だけ残す。

URL は一致しないので `deduplicator` では捕まらない。タイトルの文字バイグラム
Jaccard で測るが、**閾値だけでは真陽性と偽陽性を分離できない**（2026-09-10 に
本番 DB 全 17,218 件で総当たりして確認）:

    0.54  [Zenn]  SRE NEXT 2026に登壇・参加してきました
       || [はてブ] SRE NEXT 2026に参加しました｜maru            ← 別人の参加記
    0.43  [Qiita] なぜ、AI時代においてC#は最適な言語の1つなのか？
       || [Zenn]  なぜAI時代にGoが最適な言語なのか                ← 全くの別記事

一方、真陽性は 0.40 まで下がってくる。分離できたのは**発信主体**で、偽陽性は
すべて UGC / 個人発信のホストが絡んでいた。`_UGC_HOST_MARKERS` の除外を外すと
上の誤爆が復活するので、「単純化」で落とさないこと。除外を入れると閾値 0.45 /
窓 12 時間で 30 組 / 60 日、目視での偽陽性は 1 組まで下がる。

判定は削除ではなく `dismissed_at` を立てる。A（URL 一致）と違って判定がファジー
なので、誤爆を「非表示」ビューで確認して戻せる状態にしておく。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed

logger = logging.getLogger(__name__)

# 2026-09-10 実測。0.45 未満に下げると別記事が、上げると真陽性が落ちる
SIMILARITY_THRESHOLD: float = 0.45
# 突き合わせる時間窓（fetched_at 基準）
WINDOW_HOURS: int = 12
# 候補にする「今サイクルで新規取得された記事」の範囲。既定のフェッチ間隔は 60 分。
# これがあるので、ユーザーが手で非表示を解除した記事を毎時間再び非表示にしない
CANDIDATE_HOURS: int = 2

# UGC / 個人発信のホスト。ここを対象外にすることが判定の要（モジュール docstring 参照）。
# マーカーの形状によって異なるマッチング規則を適用:
# 1. ドット末尾のもの（blog., tech., docs.）: host.startswith(marker)
# 2. ドット含有（zenn.dev, x.com, youtube.com）: host == marker or host.endswith("." + marker)
#    （素の substring マッチは netflix.com を x.com で誤除外するため NG）
# 3. ドット非含有（hatenablog, hatenadiary）: marker in host（substring マッチで OK）
_UGC_HOST_MARKERS = (
    "zenn.dev", "qiita.com", "github.com", "note.com", "hatenablog", "hatenadiary",
    "speakerdeck.com", "anond.hatelabo.jp", "togetter.com", "posfie.com",
    "medium.com", "dev.to", "huggingface.co", "x.com", "twitter.com",
    "youtube.com", "scrapbox.io", "hamusoku.com",
)
_UGC_HOST_PREFIXES = ("blog.", "tech.", "docs.")

# タイトル比較の前に落とす記号・空白（媒体ごとの飾りを無視するため）
_TITLE_JUNK_RE = re.compile(r"[\s\-–—|｜/／【】\[\]（）()「」『』\"'’“”:：,、。.!！?？…]+")


def is_ugc_host(url: str) -> bool:
    """UGC / 個人発信のホストか。クラスタリングの対象外にする。"""
    from app.services.deduplicator import normalized_host

    host = normalized_host(url)
    if not host:
        return False
    # ドット末尾のもの（prefix）: 先頭一致
    if host.startswith(_UGC_HOST_PREFIXES):
        return True
    # ドット含有のマーカー: 完全一致または .marker で終わる
    for marker in _UGC_HOST_MARKERS:
        if "." in marker:
            if host == marker or host.endswith("." + marker):
                return True
    # ドット非含有のマーカー: substring 一致
    for marker in _UGC_HOST_MARKERS:
        if "." not in marker:
            if marker in host:
                return True
    return False


def _title_bigrams(title: str) -> frozenset[str]:
    text = _TITLE_JUNK_RE.sub("", title).lower()
    if len(text) < 2:
        return frozenset([text]) if text else frozenset()
    return frozenset(text[i : i + 2] for i in range(len(text) - 1))


def title_similarity(a: str, b: str) -> float:
    """タイトルの文字バイグラム Jaccard 係数（0.0〜1.0）。"""
    sa, sb = _title_bigrams(a), _title_bigrams(b)
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    return len(sa & sb) / union if union else 0.0


def _representative_rank(article: Article, feed_url: str | None) -> tuple:
    """クラスタの代表を選ぶソートキー。小さいほど代表にふさわしい。

    is_saved > 非引用フィード由来 > タイトルが長い方 > published_at が早い方 > id が小さい方。
    「タイトルが長い方」は dedup の優先順位に足したもの。Yahoo!ニュースのトピックス
    見出しは切り詰められるので、長い方＝元媒体の記事を残す方が読む価値が高い
    """
    from app.services.deduplicator import is_quote_feed

    return (
        not article.is_saved,
        is_quote_feed(feed_url),
        -len(article.title or ""),
        article.published_at or "",
        article.id,
    )


async def cluster_stories(session: AsyncSession) -> dict:
    """同一ニュースの別媒体版を検出し、代表以外に dismissed_at を立てる。

    候補は直近 CANDIDATE_HOURS に取り込まれた記事、突き合わせ相手は直近
    WINDOW_HOURS の記事。どちらも未読・未 dismiss・非 UGC に限る。保管済みは
    代表になれるよう突き合わせ対象には含めるが、非表示にはしない。
    候補を新規取得分に絞ってあるので、ユーザーが手で解除した記事を再び
    非表示にすることがない。

    突き合わせは各候補をプール全体（自分より id が小さい記事だけでなく）と比較する。
    id 順と fetched_at 順は通常一致するが保証はなく、一致しない組み合わせを
    `pool[:index]` のような片側走査だと取りこぼす（controller ruling 2）。
    """
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    candidate_start = (now - timedelta(hours=CANDIDATE_HOURS)).isoformat()

    rows = (
        await session.execute(
            select(Article, Feed.url.label("feed_url"))
            .join(Feed, Article.feed_id == Feed.id)
            .where(
                Article.dismissed_at.is_(None),
                Article.is_read == False,  # noqa: E712
                Article.fetched_at >= window_start,
            )
            .order_by(Article.id)
        )
    ).all()

    pool = [(article, feed_url) for article, feed_url in rows if not is_ugc_host(article.url)]
    dismissed_ids: set[int] = set()
    clusters = 0

    for article, feed_url in pool:
        if article.fetched_at < candidate_start or article.id in dismissed_ids:
            continue
        for other, other_feed_url in pool:
            if other.id == article.id or other.id in dismissed_ids:
                continue
            if other.feed_id == article.feed_id:
                continue
            if title_similarity(article.title, other.title) < SIMILARITY_THRESHOLD:
                continue

            pair = [(article, feed_url), (other, other_feed_url)]
            pair.sort(key=lambda item: _representative_rank(item[0], item[1]))
            loser = pair[1][0]
            # 保管済みは常に守る（routers/articles.py の dismiss と同じ方針）。
            # 代表選択で保管済みが先頭に来るので、ここに落ちるのは両方保管済みのときだけ
            if loser.is_saved:
                continue
            loser.dismissed_at = now.isoformat()
            dismissed_ids.add(loser.id)
            clusters += 1
            logger.info(
                "Same-story cluster: kept %r, dismissed %r", pair[0][0].title, loser.title
            )
            if loser.id == article.id:
                break

    if dismissed_ids:
        await session.commit()
    return {"clusters": clusters, "dismissed": len(dismissed_ids)}
