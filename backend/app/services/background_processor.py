"""Continuous background processor for AI article enrichment.

Replaces the APScheduler-based _summarize_job. Runs as a persistent asyncio task,
processing one article at a time to keep the LLM queue free for foreground requests.

Uses a single combined LLM call (summarize_and_tag) because the Ternary-Bonsai-8B
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
_SHORT_CONTENT_THRESHOLD = 300  # summary shorter than this triggers auto-extraction

_processor_task: asyncio.Task[None] | None = None
# Phase 0 (本文抽出) と Phase 1/2 (LLM) の skip 辞書を分離する。
# 以前は 1 つの dict を共有しており、本文抽出で 5 分 skip された記事が
# LLM 要約側でも同時に抑制されてしまい、RSS summary で要約できる記事が
# 永久にスタックしてしまう問題があった。
_extract_skip_until: dict[int, float] = {}
_llm_skip_until: dict[int, float] = {}


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
        else:
            # not_found / forbidden は恒久失敗 → WHERE で以後除外される
            article.extract_status = status
        await session.commit()

    return True


async def _process_one() -> bool:
    """Try to process one pending article. Returns True if work was done."""
    import json as _json

    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_BACKGROUND
    from app.database import async_session
    from app.models import Article, Tag

    now = time.monotonic()
    skip_ids = [aid for aid, until in _llm_skip_until.items() if until > now]

    async with async_session() as session:
        existing_names = list((await session.execute(select(Tag.name))).scalars())

        def _skip(stmt):
            return stmt.where(Article.id.not_in(skip_ids)) if skip_ids else stmt

        # Phase 1: articles needing summary + tags.
        # 候補は「本文が取得済み or これ以上抽出は試みない」記事のみ。
        # extract_status が "error" の記事は一時的障害なので Phase 0 で再試行されるまで除外。
        stmt = _skip(
            select(Article)
            .where(
                Article.ai_summary.is_(None),
                (Article.content.isnot(None))
                | (Article.extract_status.in_(["not_found", "forbidden", "skipped"])),
            )
            .order_by(Article.is_saved.desc(), Article.is_read.asc(), Article.published_at.desc())
            .limit(1)
        )
        article = (await session.execute(stmt)).scalars().first()
        if article:
            article_id = article.id
            title = article.title
            text = article.content or article.summary or ""
            phase = 1
        else:
            # Phase 2: backfill tags for already-summarized articles
            stmt = _skip(
                select(Article)
                .where(Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None))
                .order_by(Article.is_saved.desc(), Article.published_at.desc())
                .limit(1)
            )
            article = (await session.execute(stmt)).scalars().first()
            if article:
                article_id = article.id
                title = article.title
                # Use existing summary as context so combined call improves accuracy
                text = article.ai_summary or article.content or article.summary or ""
                phase = 2
            else:
                return False  # Nothing to do

    # LLM call outside session — single combined call for both summary and tags
    try:
        summary, pairs = await summarize_and_tag(
            title, text, existing_tags=existing_names, priority=PRIORITY_BACKGROUND
        )
    except Exception as e:
        logger.warning("LLM call failed (phase %d, article %d): %s", phase, article_id, e)
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
        if phase == 1:
            if summary:
                article.ai_summary = summary
            if pairs:
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])
            elif summary:
                # Summary succeeded but no tags; mark processed to avoid loop
                # (Phase 2 will retry tags later)
                pass
            else:
                # Both empty — skip this article temporarily
                _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
                return True
        else:  # phase == 2
            if pairs:
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])
                if summary:
                    article.ai_summary = summary  # update if improved
            else:
                _llm_skip_until[article_id] = time.monotonic() + _SKIP_DURATION
                return True
        await session.commit()

    logger.debug("Phase%d processed article %d: %s", phase, article_id, title[:50])
    return True


async def _run() -> None:
    from app.ai.llm_client import is_available

    while True:
        try:
            # Phase 0: content extraction — runs regardless of LLM availability
            if await _extract_one():
                continue

            if not await is_available():
                await asyncio.sleep(_SLEEP_IDLE * 3)
                continue

            did_work = await _process_one()
            if not did_work:
                await asyncio.sleep(_SLEEP_IDLE)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Background processor unexpected error: %s", e)
            await asyncio.sleep(_SLEEP_IDLE)


def start() -> None:
    global _processor_task
    _processor_task = asyncio.get_event_loop().create_task(_run(), name="background-processor")
    logger.info("Background AI processor started")


def stop() -> None:
    if _processor_task and not _processor_task.done():
        _processor_task.cancel()
    logger.info("Background AI processor stopped")


def is_running() -> bool:
    return _processor_task is not None and not _processor_task.done()
