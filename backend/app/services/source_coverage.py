"""購読フィードが自前で配信しているホストの集合を、保存済み記事から導出する。

はてなブックマークのような引用フィード（`deduplicator.is_quote_feed`）は他サイトの
記事を再配信するので、すでに購読しているサイトの記事が二重に流れてくる。ここで
求めた集合を `feed_fetcher` が使い、引用フィードからの取り込み時にスキップする。

集合を設定ファイルに持たせないのは、フィードを 1 本足したら自動で追従してほしいため。
フィード URL のホストから導く案は採っていない — ITmedia はフィードが rss.itmedia.co.jp
で記事が itmedia.co.jp なので取りこぼす。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed

# ホストを「そのフィードが配信しているサイト」とみなす下限。両方を満たす必要がある。
# 2026-09-10 実測では非引用フィードのホストはいずれも 20 件以上・占有率 92% 以上で、
# 少数混入は 1 件も無い。この下限は「たまたま 1 本混ざったホストで、そのサイト全体を
# はてブから消してしまう」のを防ぐためのもの。件数と割合を併記するのは、記事数の
# 少ないフィード（WirelessWire は 37 件）では 1% が 1 件を意味してしまうため
_MIN_HOST_ARTICLES = 3
_MIN_HOST_SHARE = 0.01


async def subscribed_hosts(session: AsyncSession) -> set[str]:
    """引用フィード以外の各フィードが配信しているホストの集合を返す。"""
    from app.services.deduplicator import is_quote_feed, normalized_host

    rows = (
        await session.execute(
            select(Feed.id, Feed.url, Article.url).join(Article, Article.feed_id == Feed.id)
        )
    ).all()

    per_feed: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for feed_id, feed_url, article_url in rows:
        if is_quote_feed(feed_url):
            continue
        if host := normalized_host(article_url):
            per_feed[feed_id][host] += 1

    hosts: set[str] = set()
    for counts in per_feed.values():
        total = sum(counts.values())
        for host, count in counts.items():
            if count >= _MIN_HOST_ARTICLES and count >= total * _MIN_HOST_SHARE:
                hosts.add(host)
    return hosts
