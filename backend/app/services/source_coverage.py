"""購読フィードが自前で配信しているホストの集合を、保存済み記事から導出する。

はてなブックマークのような引用フィード（`deduplicator.is_quote_feed`）は他サイトの
記事を再配信するので、すでに購読しているサイトの記事が二重に流れてくる。ここで
求めた集合を `feed_fetcher` が使い、引用フィードからの取り込み時にスキップする。

集合を設定ファイルに持たせないのは、フィードを 1 本足したら自動で追従してほしいため。
フィード URL のホストから導く案は採っていない — ITmedia はフィードが rss.itmedia.co.jp
で記事が itmedia.co.jp なので取りこぼす。

インポートで作られる合成フィード（`snoreader://imported`）も除外する。複数サイトの
記事が混在しているので「1 フィード = 1 サイト」前提が成り立たず、含めるとインポート
経由の無関係なホストが誤って購読済み扱いになりかねない。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed

# インポート由来の合成フィード（複数サイトの記事が混在するため、単一サイト前提の
# ホスト集合には含めない。router 層への依存を避けるためリテラルを複製している。
# 定義元: app/routers/imports.py の `_IMPORTED_FEED_URL`）
_IMPORTED_FEED_URL = "snoreader://imported"

# ホストを「そのフィードが配信しているサイト」とみなす下限。両方を満たす必要がある。
# 2026-09-10 実測では非引用フィードのホストはいずれも 20 件以上・占有率 92% 以上で、
# 少数混入は 1 件も無い。この下限は「たまたま 1 本混ざったホストで、そのサイト全体を
# はてブから消してしまう」のを防ぐためのもの。件数と割合を併記するのは、記事数の
# 少ないフィード（WirelessWire は 37 件）では 1% が 1 件を意味してしまうため
#
# 注意: この下限は「保存済み記事の件数」に対して評価される。article_cleanup.py が
# 未読・未保存の古い記事を削除していくので、更新が止まったフィードは件数が
# じわじわ減り、いずれ 3 件を割ってホストがこの集合から静かに外れうる。その場合、
# はてブ経由での同ホスト記事の取り込みスキップも気づかないうちに再開する。
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
        if is_quote_feed(feed_url) or feed_url == _IMPORTED_FEED_URL:
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
