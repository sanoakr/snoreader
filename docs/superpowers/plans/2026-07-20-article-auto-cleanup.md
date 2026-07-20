# Article Auto-Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically delete unsaved, read articles once they pass a configurable retention period (default 90 days), keeping the article table from growing unbounded while never touching saved or unread articles.

**Architecture:** A new service module `app/services/article_cleanup.py` exposes one function, `cleanup_old_articles(session)`, that deletes matching `Article` rows and returns the count deleted. It is wired into the existing `fetch_all_feeds()` pipeline in `app/services/feed_fetcher.py`, immediately after the existing `dedup_articles(session)` call, so cleanup runs once per scheduled fetch cycle — no new scheduler job is needed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, SQLite (aiosqlite), pytest + pytest-asyncio, httpx `ASGITransport` for in-process API tests.

## Global Constraints

- Retention period default: 90 days, overridable via env var `SNOREADER_ARTICLE_RETENTION_DAYS` (spec: `docs/superpowers/specs/2026-07-20-article-auto-cleanup-design.md`).
- Deletion criteria: `is_saved == False` AND `is_read == True` AND `COALESCE(published_at, fetched_at) < now - retention_days`.
- Unread articles are never deleted regardless of age. Saved articles are never deleted regardless of read state or age.
- No new scheduler job — cleanup runs inside the existing `fetch_all_feeds()` flow, after dedup.
- No user-facing confirmation UI, no soft-delete/trash mechanism, no frontend changes.
- Follow existing repo conventions: routers/services import `app.ai`/sibling `app.services` modules lazily inside function bodies — not relevant here since `article_cleanup.py` only imports `app.config` and `app.models`, which are imported normally at module top-level per `CLAUDE.md`.

---

### Task 1: Article cleanup service + retention setting

**Files:**
- Modify: `backend/app/config.py`
- Create: `backend/app/services/article_cleanup.py`
- Create: `backend/tests/test_article_cleanup.py`

**Interfaces:**
- Produces: `app.config.settings.article_retention_days: int` (default `90`, env var `SNOREADER_ARTICLE_RETENTION_DAYS`)
- Produces: `async def cleanup_old_articles(session: AsyncSession) -> int` in `app.services.article_cleanup` — deletes matching articles, commits if any were deleted, returns count deleted.

- [ ] **Step 1: Add the retention setting to `app/config.py`**

Modify `backend/app/config.py`:

```python
class Settings(BaseSettings):
    database_url: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent.parent / 'data' / 'snoreader.db'}"
    feed_fetch_interval_minutes: int = 60
    article_retention_days: int = 90
    host: str = "0.0.0.0"
    port: int = 8000
```

(Insert `article_retention_days: int = 90` directly below `feed_fetch_interval_minutes`.)

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_article_cleanup.py`:

```python
"""未保存・既読記事の保持期間経過後の自動削除機能のテスト。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


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


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


async def _make_feed(session, url: str, title: str = ""):
    from app.models import Feed

    feed = Feed(url=url, title=title or None)
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, url: str, **kwargs):
    from app.models import Article

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=url,
        title=kwargs.pop("title", "Title"),
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_cleanup_deletes_old_unsaved_read_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_cleanup_keeps_saved_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=True, is_saved=True,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_unread_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=False, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_keeps_recent_unsaved_read_article(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/recent",
            published_at=_iso(89), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 0

        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1


@pytest.mark.asyncio
async def test_cleanup_falls_back_to_fetched_at_when_published_at_null(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=None, fetched_at=_iso(91), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_cleanup_respects_retention_days_override(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SNOREADER_ARTICLE_RETENTION_DAYS を短く上書きすると、より新しい記事も削除対象になる。"""
    from app import config as config_module
    from app.database import async_session
    from app.models import Article
    from app.services.article_cleanup import cleanup_old_articles

    monkeypatch.setattr(config_module.settings, "article_retention_days", 10)

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/mid",
            published_at=_iso(15), is_read=True, is_saved=False,
        )
        await session.commit()

    async with async_session() as session:
        deleted = await cleanup_old_articles(session)
        assert deleted == 1

        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_article_cleanup.py -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'app.services.article_cleanup'` (and the retention-days test fails once that import error is fixed, since the function doesn't exist yet).

- [ ] **Step 4: Implement `cleanup_old_articles`**

Create `backend/app/services/article_cleanup.py`:

```python
"""未保存かつ既読の記事を、保持期間を過ぎた時点で自動削除する。

読み終えて保存していない記事は一時的な情報とみなし、ストレージの肥大化を
防ぐため一定期間後に削除する。保存済み記事や未読記事は対象外。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Article

logger = logging.getLogger(__name__)


async def cleanup_old_articles(session: AsyncSession) -> int:
    """保持期間を過ぎた未保存の既読記事を削除し、削除件数を返す。

    基準日時は published_at を優先し、NULL の場合は fetched_at を使う。
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=settings.article_retention_days)
    ).isoformat()

    stmt = select(Article).where(
        Article.is_saved == False,  # noqa: E712
        Article.is_read == True,  # noqa: E712
        func.coalesce(Article.published_at, Article.fetched_at) < cutoff,
    )
    targets = (await session.execute(stmt)).scalars().all()

    for article in targets:
        await session.delete(article)

    deleted = len(targets)
    if deleted:
        await session.commit()

    logger.info(
        "Cleaned up %d old unsaved articles (retention=%d days)",
        deleted,
        settings.article_retention_days,
    )
    return deleted
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_article_cleanup.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/services/article_cleanup.py backend/tests/test_article_cleanup.py
git commit -m "feat: auto-delete unsaved read articles past retention period"
```

---

### Task 2: Wire cleanup into the feed fetch cycle

**Files:**
- Modify: `backend/app/services/feed_fetcher.py:142-165` (the `fetch_all_feeds` function)
- Modify: `backend/tests/test_article_cleanup.py` (add one integration test)

**Interfaces:**
- Consumes: `cleanup_old_articles(session: AsyncSession) -> int` from Task 1 (`app.services.article_cleanup`)

- [ ] **Step 1: Write the failing integration test**

Add to `backend/tests/test_article_cleanup.py` (append at the end of the file):

```python
@pytest.mark.asyncio
async def test_fetch_all_feeds_runs_cleanup(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch_all_feeds() の実行後、保持期間を過ぎた未保存既読記事が削除されていること。

    実際の RSS フェッチ (HTTP) はこのテストの対象外のため fetch_feed をスタブ化し、
    dedup/cleanup の配線のみを検証する。
    """
    from app.database import async_session
    from app.models import Article
    from app.services import feed_fetcher

    async def _noop_fetch_feed(feed, session) -> int:
        return 0

    monkeypatch.setattr(feed_fetcher, "fetch_feed", _noop_fetch_feed)

    async with async_session() as session:
        feed = await _make_feed(session, "https://a.example.com/feed")
        await _make_article(
            session, feed.id, "g1", "https://news.example.com/old",
            published_at=_iso(91), is_read=True, is_saved=False,
        )
        await session.commit()

    await feed_fetcher.fetch_all_feeds()

    async with async_session() as session:
        remaining = (await session.execute(select(Article))).scalars().all()
        assert remaining == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_article_cleanup.py::test_fetch_all_feeds_runs_cleanup -v`
Expected: FAIL — the article is still present because `fetch_all_feeds()` does not yet call `cleanup_old_articles`.

- [ ] **Step 3: Wire `cleanup_old_articles` into `fetch_all_feeds`**

In `backend/app/services/feed_fetcher.py`, modify the `fetch_all_feeds` function (currently at lines 142-165):

```python
async def fetch_all_feeds() -> None:
    """Fetch all feeds with parallel HTTP requests (max 5 concurrent)."""
    import asyncio

    from app.database import async_session
    from app.services.article_cleanup import cleanup_old_articles

    async with async_session() as session:
        result = await session.execute(select(Feed))
        feed_ids = [f.id for f in result.scalars().all()]

    sem = asyncio.Semaphore(5)

    async def _fetch_one(feed_id: int) -> None:
        async with sem:
            async with async_session() as sess:
                feed = await sess.get(Feed, feed_id)
                if feed:
                    await fetch_feed(feed, sess)

    await asyncio.gather(*[_fetch_one(fid) for fid in feed_ids])

    async with async_session() as session:
        await dedup_articles(session)
        await cleanup_old_articles(session)
```

(Two changes: add the lazy `from app.services.article_cleanup import cleanup_old_articles` import alongside the existing lazy `async_session` import, and add the `await cleanup_old_articles(session)` line after `await dedup_articles(session)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_article_cleanup.py -v`
Expected: PASS (7 tests total)

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `cd backend && uv run pytest`
Expected: PASS (all existing tests plus the new ones, no regressions)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/feed_fetcher.py backend/tests/test_article_cleanup.py
git commit -m "feat: run article cleanup after every feed fetch cycle"
```

---

## Post-Implementation Notes

- No frontend changes are required — the existing article list/filter UI simply reflects the post-cleanup state.
- No changes to `docs/` beyond this plan and its spec; `CLAUDE.md`'s "Backend process model" section describes `fetch_all_feeds()`'s dedup step — a future doc pass could mention cleanup there too, but that's optional polish, not required for this feature to work correctly.
