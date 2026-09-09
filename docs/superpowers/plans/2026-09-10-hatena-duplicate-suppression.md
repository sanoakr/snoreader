# はてなブックマーク由来の重複記事の抑制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** はてなブックマークフィード由来の重複記事を 3 つの経路（URL 表記ゆれ・購読済みサイトの再露出・同一ニュースの別媒体版）で抑制する。

**Architecture:** 3 つとも `fetch_all_feeds()` の 1 サイクルに収める。A は既存 `normalize_url` の規則追加なので既存 dedup にそのまま乗る。C は `fetch_feed` の取り込みループでスキップする。B は新規モジュール `story_clusterer.py` が dedup の直後に走り、代表以外に `dismissed_at` を立てる。LLM は一切使わない。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (async) / SQLite、pytest + pytest-asyncio、React 19 + TypeScript + TanStack Query

**Spec:** `docs/superpowers/specs/2026-09-10-hatena-duplicate-suppression-design.md`

## Global Constraints

- 作業フローは `AGENTS.md` に従う。ブランチは既に `feat/hatena-duplicate-suppression` が切ってある（`main` に直接コミットしない）
- コメントは日本語、識別子は英語。型アノテーションを付ける
- バックエンドの lint/format ツールは無い。既存ファイルのスタイルに合わせる
- ルータおよびサービス間の相互 import は**関数の中で遅延 import** する（`app.models` / `app.database` / `app.schemas` はトップレベルで可）
- テストは LLM を呼ばない。実サーバに依存しない
- 設定は `app/config.py` の `Settings` に生やす。env のプレフィックスは `SNOREADER_`
- マジックナンバーは必ずモジュール定数にし、**実測値であること・測定日 (2026-09-10)** をコメントに残す
- バックエンドテスト: `cd backend && .venv/bin/python -m pytest`
- フロントエンド型チェック: `cd frontend && npx tsc --noEmit`
- リリースバージョンは **v0.14.0**（`backend/pyproject.toml` と `frontend/package.json` を同時に上げ、両 lockfile を再生成する）

## 仕様からの逸脱（1 点、意図的）

spec の C 節は「サイドバーの フィード ⚙ にオン/オフのトグル」と書いているが、この計画では
**`app/config.py` の設定 (`SNOREADER_SKIP_SUBSCRIBED_HOSTS_IN_QUOTE_FEEDS`, 既定 `true`) + フィード ⚙ での読み取り専用表示**として実装する。理由:

- 実行時に書き換え可能な設定を持つ仕組みがこのアプリに無く、この 1 個の真偽値のために設定テーブルを新設することになる
- `SNOREADER_ARTICLE_RETENTION_DAYS` / `SNOREADER_GENRE_UNREAD_LIMIT` など、既存の挙動設定はすべて env のみで UI トグルを持たない。そちらに揃える

ユーザーが見えるべき情報（有効かどうか・どのホストが対象か）は Task 7 の読み取り専用パネルで満たす。

## File Structure

| ファイル | 責務 |
|---|---|
| `backend/app/services/deduplicator.py`（変更） | URL 正規化規則の追加（A）、`normalized_host()` の切り出し、`_is_hatena` → `is_quote_feed` 公開名化 |
| `backend/app/services/source_coverage.py`（新規） | 購読フィードが自前で配信しているホスト集合の導出（C の判定材料のみ。DB 書き込みはしない） |
| `backend/app/services/feed_fetcher.py`（変更） | C の適用（取り込みループでのスキップ）、B の呼び出し |
| `backend/app/services/story_clusterer.py`（新規） | B。タイトル類似度の純関数群と、`dismissed_at` を立てる DB 処理 |
| `backend/app/config.py`（変更） | C のオン/オフ設定 |
| `backend/app/routers/feeds.py`（変更） | `GET /feeds/subscribed-hosts`（読み取り専用） |
| `backend/app/schemas.py`（変更） | 上記のレスポンススキーマ |
| `frontend/src/hooks/useSubscribedHosts.ts`（新規） | 上記エンドポイントの取得フック |
| `frontend/src/components/layout/FeedSidebar.tsx`（変更） | フィード ⚙ パネルへの表示追加 |

---

### Task 1: A — `normalize_url` の表記ゆれ吸収

**Files:**
- Modify: `backend/app/services/deduplicator.py`
- Test: `backend/tests/test_deduplicator.py`（追記）

**Interfaces:**
- Consumes: なし
- Produces: `normalize_url(url: str) -> str`（シグネチャ不変、挙動のみ拡張）、`normalized_host(url: str) -> str`（新規公開。Task 3・Task 4 が使う）

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_deduplicator.py` の「`--- サービス層 / エンドポイントの統合テスト ---`」コメントの**直前**に追記する。

```python
# --- A: URL 表記ゆれの吸収（2026-09-10 実測、backend/data の全 17,218 件で誤爆 0 件） ---

def test_normalize_url_maps_mobile_subdomain_alias():
    a = normalize_url("https://s.japanese.joins.com/JArticle/352523?sectcode=A00")
    b = normalize_url("https://japanese.joins.com/JArticle/352523?sectcode=A00")
    assert a == b


def test_normalize_url_rewrites_jiji_sp_path():
    a = normalize_url("https://www.jiji.com/sp/article?k=2026071600709&g=pol")
    b = normalize_url("https://www.jiji.com/jc/article?k=2026071600709&g=pol")
    assert a == b


def test_normalize_url_rewrites_jiji_sp_path_for_v8():
    a = normalize_url("https://www.jiji.com/sp/v8?id=20260824occupied_jp_photo")
    b = normalize_url("https://www.jiji.com/jc/v8?id=20260824occupied_jp_photo")
    assert a == b


def test_normalize_url_rewrites_livedoor_lite_path():
    a = normalize_url("https://news.livedoor.com/lite/article_detail/32156268/")
    b = normalize_url("https://news.livedoor.com/article/detail/32156268/")
    assert a == b


def test_normalize_url_rewrites_nikkansports_mobile_path():
    a = normalize_url(
        "https://www.nikkansports.com/m/entertainment/news/202608160000671_m.html"
    )
    b = normalize_url(
        "https://www.nikkansports.com/entertainment/news/202608160000671.html"
    )
    assert a == b


def test_normalize_url_strips_trailing_page_segment():
    a = normalize_url("https://omocoro.jp/kiji/580351/2/")
    b = normalize_url("https://omocoro.jp/kiji/580351/")
    assert a == b


def test_normalize_url_keeps_date_path_with_day_segment():
    """日付パスの「日」を末尾ページ番号と誤認しない（実測での誤爆ケース）。

    素朴に「末尾の 1〜2 桁の数字セグメントを除去」すると
    onaji.me/entry/2026/08/{18,21,24} が同一キーに潰れ、別記事 3 件が
    1 件にマージされる。直前セグメントが 1〜2 桁の数字なら剥がさない。
    """
    keys = {
        normalize_url("https://onaji.me/entry/2026/08/18"),
        normalize_url("https://onaji.me/entry/2026/08/21"),
        normalize_url("https://onaji.me/entry/2026/08/24"),
    }
    assert len(keys) == 3


def test_normalize_url_keeps_single_digit_date_path():
    a = normalize_url("https://onaji.me/entry/2026/08/3")
    b = normalize_url("https://onaji.me/entry/2026/08")
    assert a != b


def test_normalize_url_strips_page_query():
    a = normalize_url("https://toyokeizai.net/articles/-/952119?page=3")
    b = normalize_url("https://toyokeizai.net/articles/-/952119")
    assert a == b


def test_normalize_url_strips_blank_junk_params():
    base = "https://qiita.com/u/items/abc"
    assert normalize_url(f"{base}?__readwiseLocation=") == normalize_url(base)
    assert normalize_url(f"{base}?DETAIL") == normalize_url(base)
    assert normalize_url(f"{base}?timestamp=1783857343") == normalize_url(base)


def test_normalize_url_strips_p_all_but_keeps_other_p_values():
    a = normalize_url("https://www.j-cast.com/2026/08/14517135.html?p=all")
    b = normalize_url("https://www.j-cast.com/2026/08/14517135.html")
    assert a == b
    # ?p= は WordPress の記事 ID にも使われるので値が all のときだけ落とす
    assert normalize_url("https://example.com/?p=123") != normalize_url("https://example.com/")


def test_normalized_host_strips_www_and_applies_alias():
    from app.services.deduplicator import normalized_host

    assert normalized_host("https://www.itmedia.co.jp/news/x.html") == "itmedia.co.jp"
    assert normalized_host("https://www.asahi.com/a.html") == "digital.asahi.com"
    assert normalized_host("not a url") == ""
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_deduplicator.py -v`
Expected: 上で追加した 12 件が FAIL（`normalized_host` は ImportError、残りはキー不一致）

- [ ] **Step 3: `deduplicator.py` を実装**

ファイル冒頭の import に `re` を足す。

```python
import asyncio
import re
from urllib.parse import parse_qsl, urlencode, urlsplit
```

`_TRACKING_PARAMS` に 4 つ追加する（既存の要素は消さない）。

```python
_TRACKING_PARAMS = {
    "gclid", "dclid", "gbraid", "wbraid",  # Google Ads
    "fbclid", "igshid", "igsh",  # Meta
    "yclid", "msclkid", "twclid",  # Yahoo! / Microsoft / Twitter
    "mc_cid", "mc_eid",  # Mailchimp
    "_hsenc", "_hsmi", "mkt_tok",  # HubSpot / Marketo
    "n_cid", "cx_testId",  # 国内メディア配信系
    "display",  # toyokeizai.net / newsweekjapan.jp 等の表示モード切替 (?display=b)。同一記事
    # 以下 2026-09-10 追加。いずれも実データで同一記事の別 URL を作っていたもの
    "page",  # toyokeizai / togetter / dot.asahi のページ送り
    "timestamp",  # querie.me
    "DETAIL",  # news-postseven（値なし）
    "__readwiseLocation",  # Readwise 経由の共有 URL（値なし）
}
```

`_DOMAIN_ALIASES` に 1 行追加する。

```python
_DOMAIN_ALIASES = {
    "asahi.com": "digital.asahi.com",
    "delete-all.hatenablog.com": "soredoko.jp",
    # モバイル版サブドメイン。m./s./sp. の一括除去は全 17,218 件でこの 1 件しか
    # 効かず、m.media-amazon.com のような無関係ホストを別ホストに潰すので個別に列挙する
    "s.japanese.joins.com": "japanese.joins.com",
}
```

`_HATENA_MARKER` の下に、新しい定数を 3 つ足す。

```python
# ホスト別のパス書き換え。モバイル版・軽量版の URL を正規版に寄せる。
# _DOMAIN_ALIASES を引いた後のホストで参照する
_PATH_REWRITES: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "jiji.com": ((re.compile(r"^/sp/"), "/jc/"),),
    "news.livedoor.com": ((re.compile(r"^/lite/article_detail/"), "/article/detail/"),),
    "nikkansports.com": (
        (re.compile(r"^/m/"), "/"),
        (re.compile(r"_m\.html$"), ".html"),
    ),
}

# 末尾のページ番号セグメント。/2〜/9 に限り、かつ直前セグメントが 1〜2 桁の数字
# **でない**ときだけ剥がす。素朴に「末尾の 1〜2 桁の数字を除去」すると
# onaji.me/entry/2026/08/{18,21,24} が 1 キーに潰れて別記事 3 件がマージされる
# （2026-09-10 実測）。取りこぼす方向に倒してある
_TRAILING_PAGE_RE = re.compile(r"^(?P<head>/.*/(?P<prev>[^/]+))/[2-9]$")
_SHORT_NUMBER_RE = re.compile(r"\d{1,2}")
```

`normalize_url` のホスト部分を関数に切り出し、パス処理を差し込む。

```python
def normalized_host(url: str) -> str:
    """比較用のホスト名。www. を落とし、既知のドメイン別名を解決する。

    パースに失敗したら空文字を返す（呼び出し側は「ホスト不明」として扱う）。
    """
    if not url:
        return ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return _DOMAIN_ALIASES.get(host, host)


def normalize_url(url: str) -> str:
    """記事 URL を重複判定用の比較キーに変換する。

    フェッチ可能な URL ではなく比較専用の文字列を返す。パースに失敗した場合は
    入力をそのまま返す（正規化を諦めても重複検出を壊さないため）。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        host = normalized_host(url)
        if parts.port and not (
            (parts.scheme == "http" and parts.port == 80)
            or (parts.scheme == "https" and parts.port == 443)
        ):
            host = f"{host}:{parts.port}"

        path = parts.path
        for pattern, repl in _PATH_REWRITES.get(host, ()):
            path = pattern.sub(repl, path)
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        trailing = _TRAILING_PAGE_RE.match(path)
        if trailing and not _SHORT_NUMBER_RE.fullmatch(trailing.group("prev")):
            path = trailing.group("head")

        query_pairs = sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PREFIXES)
            and k not in _TRACKING_PARAMS
            # ?p= は WordPress の記事 ID にも使われるので値が all のときだけ落とす
            and not (k == "p" and v == "all")
        )
        query = urlencode(query_pairs)

        key = f"{host}{path}"
        return f"{key}?{query}" if query else key
    except Exception:
        return url
```

- [ ] **Step 4: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_deduplicator.py -v`
Expected: 全 PASS（既存テストも含む。特に `test_normalize_url_keeps_non_tracking_query` と `test_normalize_url_applies_domain_alias` が壊れていないこと）

- [ ] **Step 5: 全体テストで退行がないことを確認**

Run: `cd backend && .venv/bin/python -m pytest`
Expected: 全 PASS

- [ ] **Step 6: コミット**

```bash
git add backend/app/services/deduplicator.py backend/tests/test_deduplicator.py
git commit -m "fix: fold mobile/paged URL variants into the dedup key"
```

---

### Task 2: `is_quote_feed()` の公開名化

`_is_hatena` は Task 3・Task 5 からも使うので、モジュール公開名に変える。挙動は変えない。

**Files:**
- Modify: `backend/app/services/deduplicator.py`
- Test: `backend/tests/test_deduplicator.py`（追記）

**Interfaces:**
- Consumes: なし
- Produces: `is_quote_feed(feed_url: str | None) -> bool`（Task 3・Task 4・Task 5 が使う）

- [ ] **Step 1: 失敗するテストを書く**

Task 1 で追加したブロックの末尾に追記する。

```python
def test_is_quote_feed_detects_hatena_bookmark():
    from app.services.deduplicator import is_quote_feed

    assert is_quote_feed("https://b.hatena.ne.jp/hotentry.rss") is True
    assert is_quote_feed("https://zenn.dev/feed") is False
    assert is_quote_feed(None) is False
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_deduplicator.py::test_is_quote_feed_detects_hatena_bookmark -v`
Expected: FAIL（ImportError: cannot import name 'is_quote_feed'）

- [ ] **Step 3: 改名する**

`deduplicator.py`:

```python
def is_quote_feed(feed_url: str | None) -> bool:
    """他サイトの記事を再配信する引用フィードか（現状ははてなブックマークのみ）。"""
    return _HATENA_MARKER in (feed_url or "")
```

`dedup_articles` の中の呼び出しも直す。

```python
            rows.sort(
                key=lambda row: (
                    not row[0].is_saved,
                    is_quote_feed(row[1]),
                    row[0].fetched_at,
                    row[0].id,
                )
            )
```

`_is_hatena` は削除する。他に参照が無いことを確認する:

Run: `cd backend && grep -rn "_is_hatena" app tests`
Expected: 出力なし

- [ ] **Step 4: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_deduplicator.py -v`
Expected: 全 PASS

- [ ] **Step 5: コミット**

```bash
git add backend/app/services/deduplicator.py backend/tests/test_deduplicator.py
git commit -m "refactor: rename _is_hatena to is_quote_feed for reuse"
```

---

### Task 3: C — 購読済みホストの導出

**Files:**
- Create: `backend/app/services/source_coverage.py`
- Test: `backend/tests/test_source_coverage.py`

**Interfaces:**
- Consumes: `deduplicator.is_quote_feed`, `deduplicator.normalized_host`
- Produces: `subscribed_hosts(session: AsyncSession) -> set[str]`（Task 4 が使う）、定数 `_MIN_HOST_ARTICLES: int`, `_MIN_HOST_SHARE: float`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_source_coverage.py` を新規作成する。

```python
"""購読フィードが自前で配信しているホスト集合の導出のテスト。

引用フィード（はてなブックマーク）から、すでに購読しているサイトの記事を
取り込まないようにするための判定材料。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


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


async def _make_feed(session, url: str):
    from app.models import Feed

    feed = Feed(url=url)
    session.add(feed)
    await session.flush()
    return feed


async def _make_articles(session, feed_id: int, base_url: str, count: int, *, start: int = 0):
    from app.models import Article
    from app.services.deduplicator import normalize_url

    for i in range(start, start + count):
        url = f"{base_url}/{i}"
        session.add(
            Article(
                feed_id=feed_id,
                guid=f"{feed_id}-{url}",
                url=url,
                normalized_url=normalize_url(url),
                title=f"t{i}",
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_subscribed_hosts_collects_dominant_host(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, feed.id, "https://zenn.dev/u/articles", 10)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"zenn.dev"}


@pytest.mark.asyncio
async def test_subscribed_hosts_ignores_quote_feeds(client: AsyncClient) -> None:
    """はてブ自身が配信するホストは購読済み扱いにしない（全部消えてしまう）。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await _make_articles(session, feed.id, "https://togetter.com/li", 50)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == set()


@pytest.mark.asyncio
async def test_subscribed_hosts_requires_minimum_article_count(client: AsyncClient) -> None:
    """混入 1〜2 件のホストでそのサイト全体を巻き添えにしない。

    WirelessWire のような小さいフィード（実測 37 件）では 1% が 1 件を意味してしまい、
    割合だけでは下限にならないので件数下限を併記している。
    """
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://wirelesswire.jp/feed/")
        await _make_articles(session, feed.id, "https://wirelesswire.jp/a", 35)
        await _make_articles(session, feed.id, "https://note.com/x", 2)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"wirelesswire.jp"}


@pytest.mark.asyncio
async def test_subscribed_hosts_requires_minimum_share(client: AsyncClient) -> None:
    """件数下限は超えても、そのフィードの 1% 未満なら混入とみなす。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://gigazine.net/news/rss_2.0/")
        await _make_articles(session, feed.id, "https://gigazine.net/news", 1000)
        await _make_articles(session, feed.id, "https://note.com/x", 4)
        await session.commit()

    async with async_session() as session:
        assert await subscribed_hosts(session) == {"gigazine.net"}


@pytest.mark.asyncio
async def test_subscribed_hosts_keeps_subdomains_separate(client: AsyncClient) -> None:
    """itmedia.co.jp を購読していても nlab.itmedia.co.jp は別ホスト扱い。"""
    from app.database import async_session
    from app.services.source_coverage import subscribed_hosts

    async with async_session() as session:
        feed = await _make_feed(session, "https://rss.itmedia.co.jp/rss/1.0/topstory.xml")
        await _make_articles(session, feed.id, "https://www.itmedia.co.jp/news", 100)
        await _make_articles(session, feed.id, "https://monoist.itmedia.co.jp/mn", 10)
        await session.commit()

    async with async_session() as session:
        hosts = await subscribed_hosts(session)

    assert hosts == {"itmedia.co.jp", "monoist.itmedia.co.jp"}
    assert "nlab.itmedia.co.jp" not in hosts
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py -v`
Expected: 5 件すべて FAIL（ModuleNotFoundError: app.services.source_coverage）

- [ ] **Step 3: `source_coverage.py` を実装**

```python
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
```

- [ ] **Step 4: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py -v`
Expected: 5 件 PASS

- [ ] **Step 5: コミット**

```bash
git add backend/app/services/source_coverage.py backend/tests/test_source_coverage.py
git commit -m "feat: derive the host set each subscribed feed serves"
```

---

### Task 4: C — 取り込み時のスキップ

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/feed_fetcher.py`
- Test: `backend/tests/test_source_coverage.py`（追記）

**Interfaces:**
- Consumes: `source_coverage.subscribed_hosts`, `deduplicator.is_quote_feed`, `deduplicator.normalized_host`
- Produces: `settings.skip_subscribed_hosts_in_quote_feeds: bool`（Task 6 が読む）。`fetch_feed` のシグネチャは不変

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_source_coverage.py` の末尾に追記する。`fetch_feed` は HTTP を叩くので、`httpx.AsyncClient.get` を差し替えて固定の RSS を返す。

```python
_RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>quote</title>
{items}
</channel></rss>"""


def _rss(urls: list[str]) -> str:
    items = "\n".join(
        f"<item><title>t{i}</title><link>{u}</link><guid>{u}</guid></item>"
        for i, u in enumerate(urls)
    )
    return _RSS_TEMPLATE.format(items=items)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


def _patch_http(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    import httpx

    async def _get(self, url, *args, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse(body)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)


@pytest.mark.asyncio
async def test_fetch_skips_subscribed_hosts_in_quote_feed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article
    from app.services.feed_fetcher import fetch_feed

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        quote = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await session.commit()
        quote_id = quote.id

    _patch_http(
        monkeypatch,
        _rss(
            [
                "https://zenn.dev/other/articles/aaa",  # 購読済みホスト -> スキップ
                "https://togetter.com/li/1",  # 購読していない -> 取り込む
            ]
        ),
    )

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, quote_id)
        new_count = await fetch_feed(feed, session)

    assert new_count == 1

    async with async_session() as session:
        urls = (
            await session.execute(select(Article.url).where(Article.feed_id == quote_id))
        ).scalars().all()
    assert urls == ["https://togetter.com/li/1"]


@pytest.mark.asyncio
async def test_fetch_keeps_subscribed_hosts_in_non_quote_feed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """スキップは引用フィードだけの挙動。購読フィード自身は当然そのまま取り込む。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article
    from app.services.feed_fetcher import fetch_feed

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        await session.commit()
        zenn_id = zenn.id

    _patch_http(monkeypatch, _rss(["https://zenn.dev/new/articles/bbb"]))

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, zenn_id)
        assert await fetch_feed(feed, session) == 1

    async with async_session() as session:
        count = len(
            (
                await session.execute(
                    select(Article.id).where(Article.url == "https://zenn.dev/new/articles/bbb")
                )
            ).scalars().all()
        )
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_skip_can_be_disabled_by_setting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import config as config_module
    from app.database import async_session
    from app.services.feed_fetcher import fetch_feed

    monkeypatch.setattr(
        config_module.settings, "skip_subscribed_hosts_in_quote_feeds", False
    )

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        quote = await _make_feed(session, "https://b.hatena.ne.jp/hotentry.rss")
        await session.commit()
        quote_id = quote.id

    _patch_http(monkeypatch, _rss(["https://zenn.dev/other/articles/aaa"]))

    async with async_session() as session:
        from app.models import Feed

        feed = await session.get(Feed, quote_id)
        assert await fetch_feed(feed, session) == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py -v`
Expected: 追加した 3 件が FAIL（1 件目は 2 件取り込まれて `new_count == 1` に失敗、3 件目は AttributeError）

- [ ] **Step 3: 設定を追加**

`backend/app/config.py` の `article_retention_days` の下に追加する。

```python
    # 引用フィード（はてなブックマーク）から、すでに購読しているサイトの記事を
    # 取り込まない。購読フィードはトレンド/人気/主要といった部分集合なので、
    # はてブにしか出ていない記事も一緒に落ちる（2026-09-10 実測で 60 日 408 件）。
    # 承知の上での既定値。False にすると以降のフェッチから元に戻る
    skip_subscribed_hosts_in_quote_feeds: bool = True
```

- [ ] **Step 4: `feed_fetcher.py` に適用**

`fetch_feed` の中、`exclude_patterns` を読んだ直後に追加する。

```python
    exclude_patterns = list(
        (await session.execute(select(ExcludePattern.pattern))).scalars()
    )

    # 引用フィード（はてブ）のときだけ、すでに購読しているサイトの記事を落とす。
    # 1 フェッチにつき 1 回だけ算出する（全記事の URL を読むので毎エントリは重い）
    from app.config import settings
    from app.services.deduplicator import is_quote_feed, normalized_host

    skip_hosts: set[str] = set()
    if settings.skip_subscribed_hosts_in_quote_feeds and is_quote_feed(feed.url):
        from app.services.source_coverage import subscribed_hosts

        skip_hosts = await subscribed_hosts(session)

    new_count = 0
    skipped_subscribed = 0
```

取り込みループの `is_excluded` の直後に足す。

```python
        if is_excluded(url, exclude_patterns):
            continue
        if skip_hosts and normalized_host(url) in skip_hosts:
            skipped_subscribed += 1
            continue
```

ログ行を差し替える。

```python
    logger.info(
        "Fetched %s: %d new articles (%d skipped as already-subscribed hosts)",
        feed.url,
        new_count,
        skipped_subscribed,
    )
```

`from app.services.deduplicator import dedup_articles, normalize_url` はトップレベルに既にあるが、`is_quote_feed` / `normalized_host` は関数内 import で足す（このモジュールは既に deduplicator をトップレベル import しているので、そちらに足しても動く。既存行に合わせてトップレベルにまとめてもよい — その場合は関数内の import 行を消すこと）。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py -v`
Expected: 8 件すべて PASS

- [ ] **Step 6: 全体テストで退行がないことを確認**

Run: `cd backend && .venv/bin/python -m pytest`
Expected: 全 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/config.py backend/app/services/feed_fetcher.py backend/tests/test_source_coverage.py
git commit -m "feat: skip already-subscribed hosts when fetching quote feeds"
```

---

### Task 5: B — タイトル類似度の純関数群

**Files:**
- Create: `backend/app/services/story_clusterer.py`
- Test: `backend/tests/test_story_clusterer.py`

**Interfaces:**
- Consumes: なし（純関数のみ。DB 処理は Task 6）
- Produces: `title_similarity(a: str, b: str) -> float`, `is_ugc_host(url: str) -> bool`、定数 `SIMILARITY_THRESHOLD: float`, `WINDOW_HOURS: int`, `CANDIDATE_HOURS: int`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_story_clusterer.py` を新規作成する。

```python
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
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_story_clusterer.py -v`
Expected: 全件 FAIL（ModuleNotFoundError: app.services.story_clusterer）

- [ ] **Step 3: 純関数部分を実装**

```python
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

import re

# 2026-09-10 実測。0.45 未満に下げると別記事が、上げると真陽性が落ちる
SIMILARITY_THRESHOLD = 0.45
# 突き合わせる時間窓（fetched_at 基準）
WINDOW_HOURS = 12
# 候補にする「今サイクルで新規取得された記事」の範囲。既定のフェッチ間隔は 60 分。
# これがあるので、ユーザーが手で非表示を解除した記事を毎時間再び非表示にしない
CANDIDATE_HOURS = 2

# UGC / 個人発信のホスト。ここを対象外にすることが判定の要（モジュール docstring 参照）。
# 末尾がドットのものはサブドメイン接頭辞の意図で、ホスト名の先頭でのみ一致させる
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
    if any(marker in host for marker in _UGC_HOST_MARKERS):
        return True
    return host.startswith(_UGC_HOST_PREFIXES)


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
```

- [ ] **Step 4: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_story_clusterer.py -v`
Expected: 全 PASS

- [ ] **Step 5: コミット**

```bash
git add backend/app/services/story_clusterer.py backend/tests/test_story_clusterer.py
git commit -m "feat: add title-similarity primitives for same-story detection"
```

---

### Task 6: B — `cluster_stories()` と取得サイクルへの組み込み

**Files:**
- Modify: `backend/app/services/story_clusterer.py`
- Modify: `backend/app/services/feed_fetcher.py`
- Test: `backend/tests/test_story_clusterer.py`（追記）

**Interfaces:**
- Consumes: `title_similarity`, `is_ugc_host`, `SIMILARITY_THRESHOLD`, `WINDOW_HOURS`, `CANDIDATE_HOURS`, `deduplicator.is_quote_feed`
- Produces: `cluster_stories(session: AsyncSession) -> dict` — `{"clusters": int, "dismissed": int}` を返し、代表以外の `Article.dismissed_at` を埋める

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_story_clusterer.py` の末尾に追記する。

```python
# --- DB を伴うクラスタリングのテスト ---

from collections.abc import AsyncIterator  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


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

    assert result == {"clusters": 1, "dismissed": 1}

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
        assert await cluster_stories(session) == {"clusters": 0, "dismissed": 0}


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
        assert await cluster_stories(session) == {"clusters": 0, "dismissed": 0}


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
        assert await cluster_stories(session) == {"clusters": 0, "dismissed": 0}
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_story_clusterer.py -v`
Expected: DB を伴う 7 件が FAIL（ImportError: cannot import name 'cluster_stories'）

- [ ] **Step 3: `cluster_stories()` を実装**

`story_clusterer.py` の import 節（Task 5 で `from __future__ import annotations` と
`import re` だけがある）に**以下を追加**する。

```python
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Feed

logger = logging.getLogger(__name__)
```

ホスト抽出は `deduplicator.normalized_host` に任せているので `urlsplit` は import しない。

末尾に追記する本体:

```python
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

    for index, (article, feed_url) in enumerate(pool):
        if article.fetched_at < candidate_start or article.id in dismissed_ids:
            continue
        for other, other_feed_url in pool[:index]:
            if other.feed_id == article.feed_id or other.id in dismissed_ids:
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
```

- [ ] **Step 4: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_story_clusterer.py -v`
Expected: 全 PASS

- [ ] **Step 5: 取得サイクルに組み込む**

`backend/app/services/feed_fetcher.py` の `fetch_all_feeds` を編集する。dedup とは
別のセッションブロックにして、クラスタリングが例外を投げてもフェッチ自体は成功扱いにする
（`refresh_split_suggestions` と同じ方針）。

```python
    async with async_session() as session:
        await dedup_articles(session)

    # 判定はファジーなので、ここで落ちてもフェッチサイクルは成功扱いにする
    async with async_session() as session:
        try:
            from app.services.story_clusterer import cluster_stories

            await cluster_stories(session)
        except Exception:
            logger.exception("Failed to cluster same-story articles")
            await session.rollback()

    async with async_session() as session:
        await cleanup_old_articles(session)
```

- [ ] **Step 6: 全体テストで退行がないことを確認**

Run: `cd backend && .venv/bin/python -m pytest`
Expected: 全 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/services/story_clusterer.py backend/app/services/feed_fetcher.py backend/tests/test_story_clusterer.py
git commit -m "feat: keep one representative per same-story cluster"
```

---

### Task 7: 購読済みホストの読み取り専用表示

**Files:**
- Modify: `backend/app/routers/feeds.py`
- Modify: `backend/app/schemas.py`
- Create: `frontend/src/hooks/useSubscribedHosts.ts`
- Modify: `frontend/src/components/layout/FeedSidebar.tsx`
- Test: `backend/tests/test_source_coverage.py`（追記）

**Interfaces:**
- Consumes: `source_coverage.subscribed_hosts`, `settings.skip_subscribed_hosts_in_quote_feeds`
- Produces: `GET /api/feeds/subscribed-hosts` → `{"enabled": bool, "hosts": list[str]}`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_source_coverage.py` の末尾に追記する。

```python
@pytest.mark.asyncio
async def test_subscribed_hosts_endpoint(client: AsyncClient) -> None:
    from app.database import async_session

    async with async_session() as session:
        zenn = await _make_feed(session, "https://zenn.dev/feed")
        await _make_articles(session, zenn.id, "https://zenn.dev/u/articles", 10)
        await session.commit()

    res = await client.get("/api/feeds/subscribed-hosts")
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert body["hosts"] == ["zenn.dev"]
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py::test_subscribed_hosts_endpoint -v`
Expected: FAIL（422 か 404。`/feeds/{feed_id}` に食われる）

- [ ] **Step 3: スキーマを追加**

`backend/app/schemas.py` の末尾に追加する。

```python
class SubscribedHostsOut(BaseModel):
    """引用フィードから除外される「購読済みサイト」の状態（読み取り専用）。"""

    enabled: bool
    hosts: list[str]
```

- [ ] **Step 4: エンドポイントを追加**

`backend/app/routers/feeds.py` に追加する。**`@router.put("/feeds/{feed_id}")` より前**に
置くこと（`{feed_id}` は int なので実際には食われないが、宣言順を静的パス優先に保つ）。
`GET /feeds` の直後が置き場所。

```python
@router.get("/feeds/subscribed-hosts", response_model=SubscribedHostsOut)
async def get_subscribed_hosts(session: AsyncSession = Depends(get_session)):
    """引用フィード（はてブ）の取り込み時にスキップされるホストの一覧。

    オン/オフは env（SNOREADER_SKIP_SUBSCRIBED_HOSTS_IN_QUOTE_FEEDS）なので、
    ここは表示専用。
    """
    from app.config import settings
    from app.services.source_coverage import subscribed_hosts

    return SubscribedHostsOut(
        enabled=settings.skip_subscribed_hosts_in_quote_feeds,
        hosts=sorted(await subscribed_hosts(session)),
    )
```

`from app.schemas import ...` の行に `SubscribedHostsOut` を足す。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_source_coverage.py -v`
Expected: 全 PASS

- [ ] **Step 6: フロントのフックを作る**

`frontend/src/hooks/useSubscribedHosts.ts` を新規作成する。既存フックのスタイルに合わせる
（先に `frontend/src/hooks/useExcludePatterns.ts` を読んで `fetchJSON` の呼び方を揃えること）。

```typescript
import { useQuery } from '@tanstack/react-query';
import { fetchJSON } from '../api/client';

export interface SubscribedHosts {
  enabled: boolean;
  hosts: string[];
}

/** はてブ取り込み時にスキップされる「購読済みサイト」の一覧（表示専用）。 */
export function useSubscribedHosts() {
  return useQuery({
    queryKey: ['subscribed-hosts'],
    queryFn: () => fetchJSON<SubscribedHosts>('/api/feeds/subscribed-hosts'),
  });
}
```

- [ ] **Step 7: サイドバーに表示を足す**

`frontend/src/components/layout/FeedSidebar.tsx`:

import を追加する。

```typescript
import { useSubscribedHosts } from '../../hooks/useSubscribedHosts';
```

`const { data: excludePatterns } = useExcludePatterns();` の隣に追加する。

```typescript
  const { data: subscribedHosts } = useSubscribedHosts();
```

フィード ⚙ パネルの中、除外パターンのブロックの**後ろ**に追加する
（「重複記事を整理」ボタン → 除外パターン → ここ → OPML の並び）。

```tsx
            {subscribedHosts && (
              <div className="text-xs text-gray-500 dark:text-gray-400 space-y-1">
                <p>
                  はてブの重複除外:{' '}
                  {subscribedHosts.enabled ? '有効' : '無効'}
                </p>
                {subscribedHosts.enabled && subscribedHosts.hosts.length > 0 && (
                  <p className="break-all">
                    購読済みのため、はてブからは取り込まないサイト:{' '}
                    {subscribedHosts.hosts.join(', ')}
                  </p>
                )}
              </div>
            )}
```

- [ ] **Step 8: 型チェックとビルドを通す**

Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: エラーなし

- [ ] **Step 9: 実機確認**

Run: `make dev`
確認: サイドバーの「フィード」見出しの ⚙ を開くと「はてブの重複除外: 有効」と対象ホスト一覧が出る。ダークモードでも読める。

- [ ] **Step 10: コミット**

```bash
git add backend/app/routers/feeds.py backend/app/schemas.py backend/tests/test_source_coverage.py frontend/src/hooks/useSubscribedHosts.ts frontend/src/components/layout/FeedSidebar.tsx
git commit -m "feat: show which hosts are skipped in quote feeds"
```

---

### Task 8: 既存データへの反映・ドキュメント・リリース

**Files:**
- Modify: `backend/pyproject.toml`, `frontend/package.json`, `backend/uv.lock`, `frontend/package-lock.json`
- Modify: `README.md`, `README.ja.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: Task 1〜7 のすべて
- Produces: v0.14.0 のリリース

- [ ] **Step 1: 全テストを通す**

Run: `cd backend && .venv/bin/python -m pytest`
Expected: 全 PASS

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: 成功

- [ ] **Step 2: 既存データに A の新ルールを適用（dry-run で確認）**

新しい `normalize_url` は既存記事の `normalized_url` を再計算しないと効かない。
まず dry-run で件数を見る。

Run: `make backend`（別ターミナル）してから
```bash
curl -s -X POST localhost:8000/api/articles/dedup -H 'content-type: application/json' -d '{"dry_run": true}'
```
Expected: `duplicate_groups` が 18 前後（2026-09-10 の実測値。日が経っていれば増減する）

**注意**: `refresh_keys` を伴う全件 UPDATE は FTS トリガー経由で本文を再インデックスするため
本番実測で約 47 秒かかる。`main.py` の lifespan の `normalized_url` バックフィルは
「値が変わった行だけ」を UPDATE するので、そちらに任せるのが安全。上の dedup は
`refresh_keys=False` なので、**再起動後**に実行すること。

- [ ] **Step 3: README を更新**

`README.md` の Features、既存の重複整理の行（32 行目付近）の**直後**に 2 行追加する。

```markdown
- Same-story clustering — when two feeds cover the same news within 12 hours, keeps one representative (saved > non-Hatena-Bookmark > longer title > earlier publish) and dismisses the rest. Title similarity alone cannot separate a restated headline from a different blogger's post on the same topic, so UGC hosts (Zenn, Qiita, GitHub, note, hatenablog, …) are excluded from clustering. Dismissed, not deleted — the losing copy stays visible in the Dismissed view
- Quote-feed suppression — articles from sites you already subscribe to directly (derived from what each feed actually serves, so a new feed is picked up automatically) are skipped when ingesting Hatena Bookmark. Off via `SNOREADER_SKIP_SUBSCRIBED_HOSTS_IN_QUOTE_FEEDS=false`; the covered hosts are listed under the sidebar's フィード ⚙
```

`README.ja.md` の対応箇所（30 行目付近）の直後にも同じ内容の日本語版を 2 行追加する。

```markdown
- 同一ニュースのクラスタリング——12 時間以内に複数フィードが同じニュースを報じた場合、代表 1 件（保管済み > 非はてなブックマーク由来 > タイトルが長い方 > 公開が早い方）を残して他は非表示にする。タイトル類似度だけでは「言い換えた見出し」と「同じ話題を書いた別人のブログ」を分離できないため、UGC 系ホスト（Zenn・Qiita・GitHub・note・はてなブログ等）はクラスタリングの対象外。削除ではなく非表示なので、外れた側は「非表示」ビューで確認できる
- 引用フィードの重複抑制——すでに直接購読しているサイトの記事は、はてなブックマークからは取り込まない。対象サイトは各フィードが実際に配信しているホストから導出するので、フィードを足せば自動で追従する。`SNOREADER_SKIP_SUBSCRIBED_HOSTS_IN_QUOTE_FEEDS=false` で無効化でき、対象ホストはサイドバーの フィード ⚙ に一覧表示される
```

- [ ] **Step 4: CLAUDE.md を更新**

3 か所を編集する。

1. 「### Other services」の `deduplicator.py` の行の後ろに追加する:

```markdown
- `story_clusterer.py` — same-story clustering across feeds (B in `docs/superpowers/specs/2026-09-10-hatena-duplicate-suppression-design.md`). Title character-bigram Jaccard ≥ 0.45 within a 12h `fetched_at` window, cross-feed only, candidates limited to the last 2h of fetches (which is what makes it idempotent against a user's manual undismiss). **The UGC-host exclusion is load-bearing, not a filter you can simplify away**: measured over all 17,218 articles, no similarity threshold separates true positives from false ones — a different author's write-up of the same conference scores 0.54 while a genuine restated headline scores 0.40. Every false positive involved a UGC/personal host, so excluding them drops false positives to 1 in 30 pairs. Losers get `dismissed_at`, never deletion, because the judgement is fuzzy.
- `source_coverage.py` — `subscribed_hosts()`: the set of hosts each non-quote feed actually serves (≥3 articles and ≥1% of that feed). `feed_fetcher` uses it to skip articles from already-subscribed sites when ingesting a quote feed. Derived from stored articles rather than feed URLs because ITmedia's feed is on `rss.itmedia.co.jp` while its articles are on `itmedia.co.jp`. Note the subscribed feeds are curated subsets (Zenn "trending", Qiita "popular", Yahoo "top picks"), so this deliberately also drops ~408/60 days of articles that have no counterpart in the subscribed feed — that tradeoff was chosen explicitly.
```

2. `deduplicator.py` の行に一文足す:

```markdown
`_PATH_REWRITES` folds mobile/lite URL variants (jiji `sp/`↔`jc/`, livedoor `lite/article_detail`, nikkansports `/m/` + `_m.html`) into the canonical path, and a trailing `/2`–`/9` page segment is stripped **only when the preceding segment is not a 1–2 digit number** — without that guard, `onaji.me/entry/2026/08/{18,21,24}` collapse into one key and three different articles get merged (measured).
```

- [ ] **Step 5: バージョンを上げる**

`backend/pyproject.toml` の `version = "0.13.7"` → `version = "0.14.0"`
`frontend/package.json` の `"version": "0.13.7"` → `"version": "0.14.0"`

lockfile を再生成する（どちらもプロジェクトバージョンを埋め込んでいる）。

```bash
cd backend && uv lock
cd ../frontend && npm install --package-lock-only
```

- [ ] **Step 6: コミット**

```bash
git add README.md README.ja.md CLAUDE.md backend/pyproject.toml frontend/package.json backend/uv.lock frontend/package-lock.json
git commit -m "docs: document hatena duplicate suppression (v0.14.0)"
```

- [ ] **Step 7: PR を作る**

```bash
git push -u origin feat/hatena-duplicate-suppression
gh pr create --title "feat: suppress duplicate articles coming in via Hatena Bookmark" --body "$(cat <<'EOF'
## Summary
- Fold mobile/lite/paged URL variants into the dedup key (18 groups on real data, 0 false positives)
- Skip articles from already-subscribed hosts when ingesting quote feeds
- Cluster same-story articles across feeds and dismiss all but one representative

## Test plan
- [ ] Backend tests pass
- [ ] Frontend type check passes
- [ ] Sidebar フィード ⚙ shows the skipped-host list
EOF
)"
```

- [ ] **Step 8: マージ・タグ・プッシュ・本番反映**

`AGENTS.md` の 7〜11 に従う。

```bash
git checkout main && git pull origin main
git merge --no-ff feat/hatena-duplicate-suppression -m "Merge feat/hatena-duplicate-suppression: suppress hatena duplicates (v0.14.0)"
git tag -a v0.14.0 -m "v0.14.0: suppress duplicate articles coming in via Hatena Bookmark"
git push origin main && git push origin v0.14.0
make deploy
launchctl kickstart -k "gui/$(id -u)/com.ccxa.snoreader"
```

- [ ] **Step 9: 本番で動作確認**

```bash
curl -s localhost:8000/api/feeds/subscribed-hosts
curl -s -X POST localhost:8000/api/articles/dedup -H 'content-type: application/json' -d '{"dry_run": true}'
```
Expected: 前者が 13 前後のホストを返す。後者の `duplicate_groups` が 0 でない（= A が既存データに効いた）。新しい PID を `launchctl list | grep snoreader` で確認する。
