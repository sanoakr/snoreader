"""同一ニュースの別媒体版クラスタリングのテスト。

判定は決定的な関数のみ（LLM は使わない）。真陽性・偽陽性の実例は
2026-09-10 に本番 DB 全 17,218 件を総当たりして選んだもの。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

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
        "https://www.netflix.com/title/1",  # x.com substring を含むが除外しない
        "https://www.fedex.com/x",          # x.com substring を含むが除外しない
        "https://www.box.com/x",            # x.com substring を含むが除外しない
    ],
)
def test_news_hosts_are_not_excluded(url: str) -> None:
    assert is_ugc_host(url) is False


def test_publickey_path_containing_blog_is_not_ugc():
    """`blog.` はサブドメイン接頭辞の意図。パス中の /blog/ で誤判定しない。"""
    assert is_ugc_host("https://www.publickey1.jp/blog/26/mcp.html") is False


def test_similarity_ignores_punctuation_and_spacing():
    assert title_similarity("桐谷広人さん　前立腺と大腸にがん", "桐谷広人さん 前立腺と大腸にがん") == 1.0


def test_similarity_normalizes_curly_quotes():
    """カーリークォートと直線クォートは同じに正規化される。"""
    # 直線引用符: straight quotes
    straight = 'Hello "world" and \'text\''
    # カーリークォート: curly quotes U+201C, U+201D, U+2019
    curly = "Hello “world” and ’text’"
    assert title_similarity(straight, curly) == 1.0


def test_similarity_is_zero_for_unrelated_titles():
    assert title_similarity("Pixel 11 シリーズ発表", "大腸がんと腸内細菌") < SIMILARITY_THRESHOLD


def test_similarity_handles_empty_titles():
    assert title_similarity("", "") == 0.0
    assert title_similarity("", "何か") == 0.0


# --- DB を伴うクラスタリングのテスト ---


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    await database_module.engine.dispose()


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


async def _feed(session, url: str):
    from app.models import Feed

    feed = Feed(url=url)
    session.add(feed)
    await session.flush()
    return feed


async def _article(session, feed_id: int, url: str, title: str, **kwargs):
    from app.models import Article
    from app.services.deduplicator import normalize_url

    article = Article(
        feed_id=feed_id,
        guid=url,
        url=url,
        normalized_url=normalize_url(url),
        title=title,
        fetched_at=kwargs.pop("fetched_at", _iso(0.1)),
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_cluster_dismisses_the_shorter_titled_copy(client: AsyncClient) -> None:
    """Yahoo トピックスの切り詰め見出しではなく、元媒体の記事を残す。"""
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        yahoo = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        itmedia = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        short = await _article(
            session, yahoo.id, "https://news.yahoo.co.jp/pickup/1", "キオクシア 上場来高値から半値に"
        )
        long = await _article(
            session,
            itmedia.id,
            "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
        )
        await session.commit()
        short_id, long_id = short.id, long.id

    async with async_session() as session:
        result = await cluster_stories(session)

    assert result == {"dismissed": 1}

    async with async_session() as session:
        from app.models import Article

        assert (await session.get(Article, short_id)).dismissed_at is not None
        assert (await session.get(Article, long_id)).dismissed_at is None


@pytest.mark.asyncio
async def test_cluster_keeps_non_quote_feed_over_hatena(client: AsyncClient) -> None:
    """はてブ側を非表示にする。代表が新規取得側とは限らない。"""
    from app.database import async_session
    from app.models import Article
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        hatena = await _feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        techno = await _feed(session, "https://www.techno-edge.net/rss20/index.rdf")
        # はてブ側の方がタイトルが長いが、引用フィードなので負ける
        h = await _article(
            session,
            hatena.id,
            "https://www.jiji.com/jc/article?k=1",
            "【速報】前衛芸術家の草間弥生さん死去、記者会見の詳報はこちら",
            fetched_at=_iso(3),
        )
        t = await _article(
            session,
            techno.id,
            "https://www.techno-edge.net/article/2026/01/1.html",
            "【速報】前衛芸術家の草間弥生さん死去",
        )
        await session.commit()
        h_id, t_id = h.id, t.id

    async with async_session() as session:
        assert (await cluster_stories(session))["dismissed"] == 1

    async with async_session() as session:
        assert (await session.get(Article, h_id)).dismissed_at is not None
        assert (await session.get(Article, t_id)).dismissed_at is None


@pytest.mark.asyncio
async def test_cluster_does_not_group_ugc_articles(client: AsyncClient) -> None:
    """実測での偽陽性 3 例。UGC ホスト除外を外すとここが落ちる。"""
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    pairs = [
        (
            ("https://zenn.dev/a/articles/1", "SRE NEXT 2026に登壇・参加してきました"),
            ("https://mrmr.hatenablog.com/entry/1", "SRE NEXT 2026に参加しました｜maru"),
        ),
        (
            ("https://qiita.com/a/items/1", "なぜ、AI時代においてC#は最適な言語の1つなのか？"),
            ("https://zenn.dev/b/articles/2", "なぜAI時代にGoが最適な言語なのか"),
        ),
        (
            ("https://zenn.dev/c/articles/3", "CyberAgentの追加学習R1をMacで動かしてClineする"),
            ("https://huggingface.co/cyberagent/x", "cyberagent/DeepSeek-R1-Distill-Qwen-32B-Ja"),
        ),
    ]

    async with async_session() as session:
        f1 = await _feed(session, "https://zenn.dev/feed")
        f2 = await _feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        for (u1, t1), (u2, t2) in pairs:
            await _article(session, f1.id, u1, t1)
            await _article(session, f2.id, u2, t2)
        await session.commit()

    async with async_session() as session:
        assert await cluster_stories(session) == {"dismissed": 0}


@pytest.mark.asyncio
async def test_cluster_ignores_same_feed_pairs(client: AsyncClient) -> None:
    """同一フィード内の重複は A / dedup の担当。ここでは触らない。"""
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        feed = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        await _article(session, feed.id, "https://news.yahoo.co.jp/pickup/1", "同じ見出しの記事")
        await _article(session, feed.id, "https://news.yahoo.co.jp/pickup/2", "同じ見出しの記事")
        await session.commit()

    async with async_session() as session:
        assert await cluster_stories(session) == {"dismissed": 0}


@pytest.mark.asyncio
async def test_cluster_never_dismisses_saved_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        saved = await _article(
            session,
            f1.id,
            "https://news.yahoo.co.jp/pickup/1",
            "キオクシア 上場来高値から半値に",
            is_saved=True,
        )
        other = await _article(
            session,
            f2.id,
            "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
        )
        await session.commit()
        saved_id, other_id = saved.id, other.id

    async with async_session() as session:
        await cluster_stories(session)

    async with async_session() as session:
        assert (await session.get(Article, saved_id)).dismissed_at is None
        assert (await session.get(Article, other_id)).dismissed_at is not None


@pytest.mark.asyncio
async def test_cluster_never_dismisses_when_both_sides_are_saved(client: AsyncClient) -> None:
    """`if loser.is_saved: continue` の唯一の到達経路。両方保管済みなら誰も消えない。

    片方だけ保管済みのケースは `_representative_rank` の `not article.is_saved` で
    保管済みが常に代表側に回るため、このガードを削っても通ってしまう
    （test_cluster_never_dismisses_saved_articles はそれをピン留めできていない）。
    """
    from app.database import async_session
    from app.models import Article
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        a = await _article(
            session,
            f1.id,
            "https://news.yahoo.co.jp/pickup/1",
            "キオクシア 上場来高値から半値に",
            is_saved=True,
        )
        b = await _article(
            session,
            f2.id,
            "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
            is_saved=True,
        )
        await session.commit()
        a_id, b_id = a.id, b.id

    async with async_session() as session:
        assert await cluster_stories(session) == {"dismissed": 0}

    async with async_session() as session:
        assert (await session.get(Article, a_id)).dismissed_at is None
        assert (await session.get(Article, b_id)).dismissed_at is None


@pytest.mark.asyncio
async def test_cluster_is_idempotent(client: AsyncClient) -> None:
    """2 回流しても追加の非表示が発生しない。"""
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        await _article(session, f1.id, "https://news.yahoo.co.jp/pickup/1", "キオクシア 上場来高値から半値に")
        await _article(
            session,
            f2.id,
            "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
        )
        await session.commit()

    async with async_session() as session:
        assert (await cluster_stories(session))["dismissed"] == 1
    async with async_session() as session:
        assert (await cluster_stories(session))["dismissed"] == 0


@pytest.mark.asyncio
async def test_cluster_skips_articles_outside_the_candidate_window(client: AsyncClient) -> None:
    """候補は今サイクルの新規取得分だけ。古い 2 件は放置する。"""
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        await _article(
            session, f1.id, "https://news.yahoo.co.jp/pickup/1",
            "キオクシア 上場来高値から半値に", fetched_at=_iso(6),
        )
        await _article(
            session, f2.id, "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に", fetched_at=_iso(6),
        )
        await session.commit()

    async with async_session() as session:
        assert await cluster_stories(session) == {"dismissed": 0}


@pytest.mark.asyncio
async def test_cluster_matches_regardless_of_id_order(client: AsyncClient) -> None:
    """id の小さい方が新規取得（候補）でも、比較はプール全体に対して行う。

    controller ruling 2 のピン留め: `pool[:index]` のみを見る実装だと、
    候補（id が小さい・fetched_at が新しい）は自分より前の id しか見ないため
    この組み合わせを検出できず {"dismissed": 0} になってしまう。
    """
    from app.database import async_session
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        # id が小さい方を先に挿入し、かつ fetched_at を新しく（= 今サイクルの候補）する
        await _article(
            session, f1.id, "https://news.yahoo.co.jp/pickup/1",
            "キオクシア 上場来高値から半値に", fetched_at=_iso(0.1),
        )
        # id が大きい方は 3 時間前に取得済み（候補ではないが突き合わせ対象ではある）
        await _article(
            session, f2.id, "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に", fetched_at=_iso(3),
        )
        await session.commit()

    async with async_session() as session:
        assert (await cluster_stories(session))["dismissed"] == 1


@pytest.mark.asyncio
async def test_cluster_does_not_redismiss_after_candidate_window_passes(client: AsyncClient) -> None:
    """CANDIDATE_HOURS 経過後に手で解除した記事は再び非表示にならない。

    実際に保証できるのはここまで。取得直後（CANDIDATE_HOURS 以内）に解除した
    場合は次のサイクルでまだ候補扱いなので再び非表示になりうる（この設計の
    既知の限界。ユーザーが解除したことを覚える永続フラグは持たない）。
    """
    from app.database import async_session
    from app.models import Article
    from app.services.story_clusterer import cluster_stories

    async with async_session() as session:
        f1 = await _feed(session, "https://news.yahoo.co.jp/rss/topics/top-picks.xml")
        f2 = await _feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        a = await _article(
            session,
            f1.id,
            "https://news.yahoo.co.jp/pickup/1",
            "キオクシア 上場来高値から半値に",
            fetched_at=_iso(6),
            dismissed_at=_iso(5),  # 過去に非表示になっていた
        )
        await _article(
            session,
            f2.id,
            "https://www.itmedia.co.jp/news/articles/1.html",
            "キオクシア株、一時ストップ安　上場来高値から半値以下に",
            fetched_at=_iso(6),
        )
        await session.commit()
        a_id = a.id

    # ユーザーが手で解除した状態を再現
    async with async_session() as session:
        article = await session.get(Article, a_id)
        article.dismissed_at = None
        await session.commit()

    async with async_session() as session:
        assert await cluster_stories(session) == {"dismissed": 0}

    async with async_session() as session:
        assert (await session.get(Article, a_id)).dismissed_at is None
