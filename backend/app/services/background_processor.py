"""Continuous background processor for AI article enrichment.

Replaces the APScheduler-based _summarize_job. Runs as persistent asyncio tasks:
one Phase 0 (extraction) loop, _PHASE1_WORKERS concurrent Phase 1 (summary+tags)
loops on the task_queue "bulk" lane, and one Phase 2 (tag backfill) loop on the
"reserved" lane shared with foreground requests — see app/ai/task_queue.py for
why this split exists (slab-llm's Ollama backend handles a couple of concurrent
chat completions well but foreground responsiveness still needs a guaranteed-free lane).

Uses a single combined LLM call (summarize_and_tag) because the Gemma 4
model can only generate tag pairs reliably as part of a structured SUMMARY+TAGS output.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

_SLEEP_IDLE = 10
_SKIP_DURATION = 300  # seconds to skip an article after failure
_SHORT_CONTENT_THRESHOLD = 100  # summary shorter than this triggers auto-extraction
_PHASE1_WORKERS = 2  # matches task_queue's "bulk" lane worker count

_processor_tasks: list[asyncio.Task[None]] = []
# Phase 0 (本文抽出) と Phase 1/2 (LLM) の skip 辞書を分離する。
# 以前は 1 つの dict を共有しており、本文抽出で 5 分 skip された記事が
# LLM 要約側でも同時に抑制されてしまい、RSS summary で要約できる記事が
# 永久にスタックしてしまう問題があった。
_extract_skip_until: dict[int, float] = {}
_llm_skip_until: dict[int, float] = {}
# 複数の Phase 1 ループが同じ記事を同時に取得しないための in-flight リスト。
_phase1_in_flight: list[int] = []


async def _extract_one() -> bool:
    """Phase 0: auto-extract full content for articles with short/truncated summaries.

    Runs independently of LLM availability (pure HTTP + trafilatura).
    """
    from app.database import async_session
    from app.models import Article
    from app.services.content_extractor import extract_content

    now = time.monotonic()
    skip_ids = [aid for aid, until in _extract_skip_until.items() if until > now]

    async with async_session() as session:
        stmt = (
            select(Article)
            .where(
                Article.content.is_(None),
                func.coalesce(func.length(Article.summary), 0) < _SHORT_CONTENT_THRESHOLD,
                # 永続失敗 (not_found / forbidden) と ユーザー skipped 指定は対象外。
                # null = 未試行, "error" = 一時的障害 (_extract_skip_until で 5 分 backoff)
                Article.extract_status.is_(None) | (Article.extract_status == "error"),
            )
            .order_by(Article.is_saved.desc(), Article.is_read.asc(), Article.published_at.desc())
        )
        if skip_ids:
            stmt = stmt.where(Article.id.not_in(skip_ids))
        stmt = stmt.limit(1)
        article = (await session.execute(stmt)).scalars().first()
        if not article:
            return False
        article_id = article.id
        url = article.url

    content, status = await extract_content(url)

    async with async_session() as session:
        article = await session.get(Article, article_id)
        if not article:
            return True
        article.extract_attempts = (article.extract_attempts or 0) + 1
        if content:
            article.content = content
            article.extract_status = None
            logger.debug("Auto-extracted content for article %d", article_id)
        elif status == "error":
            # 一時的障害: UI 表示用に "error" を記録しつつ、5 分 backoff で再試行。
            article.extract_status = "error"
            _extract_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        elif status is None:
            # HTTP 200 だが trafilatura が空を返したケース（JS-heavy SPA / PDF など）。
            # 同じ URL を何度叩いても結果は同じなので恒久失敗扱いにして
            # Phase 1 の RSS summary フォールバックへ回す。
            article.extract_status = "empty"
        else:
            # not_found / forbidden は恒久失敗 → WHERE で以後除外される
            article.extract_status = status
        await session.commit()

    return True


async def _process_phase1_one() -> bool:
    """Phase 1: summary + tags for one article needing both (combined LLM call).

    Runs on the task_queue "bulk" lane, safe to call concurrently from multiple
    loops — an article is added to _phase1_in_flight before the first await so
    two concurrent loops never claim the same one.
    """
    import json as _json

    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_BACKGROUND
    from app.database import async_session
    from app.models import Article, Tag

    now = time.monotonic()
    skip_ids = [aid for aid, until in _llm_skip_until.items() if until > now]
    skip_ids += _phase1_in_flight

    async with async_session() as session:
        existing_names = list((await session.execute(select(Tag.name))).scalars())

        # 候補:
        #   a) 本文取得済み (content IS NOT NULL)
        #   b) 恒久抽出失敗 (not_found / forbidden / skipped / empty)
        #   c) 未抽出 (extract_status IS NULL) だが RSS summary がある
        #      → Phase 0 は summary >= 100 を対象外にするので、RSS summary だけの記事は
        #        ここで処理しないと永遠にスタックする。
        # extract_status = "error" は一時的障害なので Phase 0 再試行まで除外。
        stmt = (
            select(Article)
            .where(
                Article.ai_summary.is_(None),
                (Article.content.isnot(None))
                | (Article.extract_status.in_(["not_found", "forbidden", "skipped", "empty"]))
                | (
                    Article.extract_status.is_(None)
                    & Article.content.is_(None)
                    & Article.summary.isnot(None)
                ),
            )
            .order_by(Article.is_saved.desc(), Article.is_read.asc(), Article.published_at.desc())
        )
        if skip_ids:
            stmt = stmt.where(Article.id.not_in(skip_ids))
        stmt = stmt.limit(1)
        article = (await session.execute(stmt)).scalars().first()
        if not article:
            return False
        article_id = article.id
        title = article.title
        text = article.content or article.summary or ""

    _phase1_in_flight.append(article_id)
    try:
        try:
            summary, pairs = await summarize_and_tag(
                title, text, existing_tags=existing_names, priority=PRIORITY_BACKGROUND, lane="bulk"
            )
        except Exception as e:
            logger.warning("LLM call failed (phase 1, article %d): %s", article_id, e)
            _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
            return True

        if not summary and not pairs:
            # LLM unavailable or model returned nothing parseable
            _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
            return True

        async with async_session() as session:
            article = await session.get(Article, article_id)
            if not article:
                return True
            if summary:
                article.ai_summary = summary
            if pairs:
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])
            # summary succeeded but no tags: leave tag_suggestions unset,
            # Phase 2 will backfill it later
            await session.commit()

        logger.debug("Phase1 processed article %d: %s", article_id, title[:50])
        return True
    finally:
        _phase1_in_flight.remove(article_id)


async def _process_phase2_one() -> bool:
    """Phase 2: backfill tags for articles that already have a summary.

    Runs on the task_queue "reserved" lane, shared with foreground calls.
    """
    import json as _json

    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_BACKGROUND
    from app.database import async_session
    from app.models import Article, Tag

    now = time.monotonic()
    skip_ids = [aid for aid, until in _llm_skip_until.items() if until > now]

    async with async_session() as session:
        existing_names = list((await session.execute(select(Tag.name))).scalars())
        stmt = select(Article).where(
            Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None)
        )
        if skip_ids:
            stmt = stmt.where(Article.id.not_in(skip_ids))
        stmt = stmt.order_by(Article.is_saved.desc(), Article.published_at.desc()).limit(1)
        article = (await session.execute(stmt)).scalars().first()
        if not article:
            return False
        article_id = article.id
        title = article.title
        # Use existing summary as context so combined call improves accuracy
        text = article.ai_summary or article.content or article.summary or ""

    try:
        # Combined call is reused here too — never overwrites the existing summary.
        _, pairs = await summarize_and_tag(
            title, text, existing_tags=existing_names, priority=PRIORITY_BACKGROUND, lane="reserved"
        )
    except Exception as e:
        logger.warning("LLM call failed (phase 2, article %d): %s", article_id, e)
        _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        return True

    if not pairs:
        _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        return True

    async with async_session() as session:
        article = await session.get(Article, article_id)
        if not article:
            return True
        article.tag_suggestions = _json.dumps([en for en, _ in pairs])
        await session.commit()

    logger.debug("Phase2 processed article %d: %s", article_id, title[:50])
    return True


async def _extract_loop() -> None:
    """Phase 0: content extraction. LLM-independent, runs on its own."""
    while True:
        try:
            if not await _extract_one():
                await asyncio.sleep(_SLEEP_IDLE)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Background processor (extract) unexpected error: %s", e)
            await asyncio.sleep(_SLEEP_IDLE)


async def _phase1_loop() -> None:
    from app.ai.llm_client import is_available

    while True:
        try:
            if not await is_available():
                await asyncio.sleep(_SLEEP_IDLE * 3)
                continue
            if not await _process_phase1_one():
                await asyncio.sleep(_SLEEP_IDLE)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Background processor (phase1) unexpected error: %s", e)
            await asyncio.sleep(_SLEEP_IDLE)


async def _phase2_loop() -> None:
    from app.ai.llm_client import is_available

    while True:
        try:
            if not await is_available():
                await asyncio.sleep(_SLEEP_IDLE * 3)
                continue
            if not await _process_phase2_one():
                await asyncio.sleep(_SLEEP_IDLE)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Background processor (phase2) unexpected error: %s", e)
            await asyncio.sleep(_SLEEP_IDLE)


def start() -> None:
    global _processor_tasks
    loop = asyncio.get_event_loop()
    _processor_tasks = [
        loop.create_task(_extract_loop(), name="background-processor-extract"),
        *(
            loop.create_task(_phase1_loop(), name=f"background-processor-phase1-{i}")
            for i in range(_PHASE1_WORKERS)
        ),
        loop.create_task(_phase2_loop(), name="background-processor-phase2"),
    ]
    logger.info("Background AI processor started (%d tasks)", len(_processor_tasks))


def stop() -> None:
    for task in _processor_tasks:
        if not task.done():
            task.cancel()
    logger.info("Background AI processor stopped")


def is_running() -> bool:
    return any(not task.done() for task in _processor_tasks)
