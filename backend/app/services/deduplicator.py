"""記事 URL の正規化とフィード横断の重複記事検出・削除。

はてなブックマーク等の引用フィードは他サイトの記事をそのまま再配信するため、
同じ記事が複数フィードに別レコードとして取り込まれる。ここでは URL を正規化した
キーで重複グループを検出し、優先順位に従って 1 件だけ残して残りを削除する。
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, ArticleTag, Feed

# utm_* 系はプレフィックス一致、それ以外の主要トラッキングパラメータは列挙
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "gclid", "dclid", "gbraid", "wbraid",  # Google Ads
    "fbclid", "igshid", "igsh",  # Meta
    "yclid", "msclkid", "twclid",  # Yahoo! / Microsoft / Twitter
    "mc_cid", "mc_eid",  # Mailchimp
    "_hsenc", "_hsmi", "mkt_tok",  # HubSpot / Marketo
    "n_cid", "cx_testId",  # 国内メディア配信系
    "display",  # toyokeizai.net / newsweekjapan.jp 等の表示モード切替 (?display=b)。同一記事
}
# 意図的に除去しない: ref / source / from は GitHub 等で機能パラメータとして使われるため、
# 除去すると誤って別記事を同一視するリスクがある（見逃す方向の方が安全）。

# 既知のドメイン移行・別名（旧ホスト → 新ホスト）。www. 除去後のホストで引くのでキーに www. は含めない。
_DOMAIN_ALIASES = {
    "asahi.com": "digital.asahi.com",
    "delete-all.hatenablog.com": "soredoko.jp",
}

_HATENA_MARKER = "b.hatena.ne.jp"

# 並列フェッチ・手動リフレッシュ・手動一括掃除が同時に走らないよう直列化する
_dedup_lock = asyncio.Lock()


def normalize_url(url: str) -> str:
    """記事 URL を重複判定用の比較キーに変換する。

    フェッチ可能な URL ではなく比較専用の文字列を返す。パースに失敗した場合は
    入力をそのまま返す（正規化を諦めても重複検出を壊さないため）。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        host = _DOMAIN_ALIASES.get(host, host)
        if parts.port and not (
            (parts.scheme == "http" and parts.port == 80)
            or (parts.scheme == "https" and parts.port == 443)
        ):
            host = f"{host}:{parts.port}"

        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")

        query_pairs = sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PREFIXES) and k not in _TRACKING_PARAMS
        )
        query = urlencode(query_pairs)

        key = f"{host}{path}"
        return f"{key}?{query}" if query else key
    except Exception:
        return url


def _is_hatena(feed_url: str | None) -> bool:
    return _HATENA_MARKER in (feed_url or "")


async def _refresh_normalized_urls(session: AsyncSession) -> None:
    """正規化ルール変更に追従するため、全記事の normalized_url を再計算する。"""
    rows = (await session.execute(select(Article.id, Article.url, Article.normalized_url))).all()
    updates = [
        {"id": article_id, "normalized_url": new_key}
        for article_id, url, current in rows
        if (new_key := normalize_url(url)) != current
    ]
    for i in range(0, len(updates), 1000):
        await session.execute(update(Article), updates[i : i + 1000])
    if updates:
        await session.flush()


async def _merge_into_keeper(session: AsyncSession, keeper: Article, loser: Article) -> None:
    """loser の状態・タグ・本文を keeper にマージしてから loser を削除する。"""
    if loser.is_read and not keeper.is_read:
        keeper.is_read = True
        keeper.read_at = keeper.read_at or loser.read_at
    if loser.is_saved and not keeper.is_saved:
        keeper.is_saved = True
        keeper.saved_at = keeper.saved_at or loser.saved_at

    for field in ("content", "ai_summary", "tag_suggestions", "image_url"):
        if getattr(keeper, field) is None and getattr(loser, field) is not None:
            setattr(keeper, field, getattr(loser, field))

    loser_assocs = (
        await session.execute(select(ArticleTag).where(ArticleTag.article_id == loser.id))
    ).scalars().all()
    if loser_assocs:
        keeper_tag_ids = set(
            (
                await session.execute(
                    select(ArticleTag.tag_id).where(ArticleTag.article_id == keeper.id)
                )
            ).scalars().all()
        )
        for assoc in loser_assocs:
            if assoc.tag_id in keeper_tag_ids:
                await session.delete(assoc)
            else:
                assoc.article_id = keeper.id
                keeper_tag_ids.add(assoc.tag_id)
        await session.flush()

    await session.delete(loser)


async def dedup_articles(
    session: AsyncSession, *, dry_run: bool = False, refresh_keys: bool = False
) -> dict:
    """フィード横断の重複記事を検出し、優先順位が最も高い 1 件だけ残して削除する。

    残す優先順位: is_saved > 非はてなブックマーク由来 > fetched_at が古い方 > id が小さい方。
    """
    async with _dedup_lock:
        if refresh_keys:
            await _refresh_normalized_urls(session)

        dup_keys = (
            await session.execute(
                select(Article.normalized_url)
                .where(Article.normalized_url.isnot(None), Article.normalized_url != "")
                .group_by(Article.normalized_url)
                .having(func.count() > 1)
            )
        ).scalars().all()

        duplicate_groups = 0
        deleted = 0

        for key in dup_keys:
            rows = (
                await session.execute(
                    select(Article, Feed.url.label("feed_url"))
                    .join(Feed, Article.feed_id == Feed.id)
                    .where(Article.normalized_url == key)
                )
            ).all()
            if len(rows) < 2:
                continue
            duplicate_groups += 1

            rows.sort(
                key=lambda row: (
                    not row[0].is_saved,
                    _is_hatena(row[1]),
                    row[0].fetched_at,
                    row[0].id,
                )
            )
            keeper = rows[0][0]
            losers = [row[0] for row in rows[1:]]

            if dry_run:
                deleted += len(losers)
                continue

            for loser in losers:
                await _merge_into_keeper(session, keeper, loser)
                deleted += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

        return {"duplicate_groups": duplicate_groups, "deleted": deleted, "dry_run": dry_run}
