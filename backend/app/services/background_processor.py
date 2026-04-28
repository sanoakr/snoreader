"""Continuous background processor for AI article enrichment.

Replaces the APScheduler-based _summarize_job. Runs as a persistent asyncio task,
processing one article at a time to keep the LLM queue free for foreground requests.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

logger = logging.getLogger(__name__)

_SLEEP_IDLE = 10
_SKIP_DURATION = 300  # seconds to skip an article after failure

_processor_task: asyncio.Task[None] | None = None
_skip_until: dict[int, float] = {}


async def _process_one() -> bool:
    """Try to process one pending article. Returns True if work was done."""
    import json as _json

    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_BACKGROUND
    from app.ai.tagger import suggest_tags
    from app.database import async_session
    from app.models import Article, Tag

    now = time.monotonic()
    skip_ids = [aid for aid, until in _skip_until.items() if until > now]

    async with async_session() as session:
        existing_names = list((await session.execute(select(Tag.name))).scalars())

        def _base_skip(stmt):
            return stmt.where(Article.id.not_in(skip_ids)) if skip_ids else stmt

        # Phase 1: articles needing summary + tags
        stmt = _base_skip(
            select(Article)
            .where(Article.ai_summary.is_(None))
            .order_by(Article.is_saved.desc(), Article.is_read.asc(), Article.published_at.desc())
            .limit(1)
        )
        article = (await session.execute(stmt)).scalars().first()
        if article:
            article_id, title, text, phase = (
                article.id, article.title,
                article.content or article.summary or "", 1,
            )
        else:
            # Phase 2: backfill tags for already-summarized articles
            stmt = _base_skip(
                select(Article)
                .where(Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None))
                .order_by(Article.is_saved.desc(), Article.published_at.desc())
                .limit(1)
            )
            article = (await session.execute(stmt)).scalars().first()
            if article:
                article_id, title, text, phase = (
                    article.id, article.title, article.ai_summary or "", 2,
                )
            else:
                # Phase 3: tag suggestions for unread/unsaved recommendation candidates
                stmt = _base_skip(
                    select(Article)
                    .where(
                        Article.is_read == False,  # noqa: E712
                        Article.is_saved == False,  # noqa: E712
                        Article.tag_suggestions.is_(None),
                    )
                    .order_by(Article.published_at.desc())
                    .limit(1)
                )
                article = (await session.execute(stmt)).scalars().first()
                if article:
                    article_id, title, text, phase = (
                        article.id, article.title,
                        article.ai_summary or article.content or article.summary or "", 3,
                    )
                else:
                    return False  # Nothing to do

    # LLM call outside session so the connection is free during the long call
    try:
        if phase == 1:
            summary, pairs = await summarize_and_tag(
                title, text, existing_tags=existing_names, priority=PRIORITY_BACKGROUND
            )
        else:
            summary = None
            pairs = await suggest_tags(
                title, text, existing_tags=existing_names, priority=PRIORITY_BACKGROUND
            )
    except Exception as e:
        logger.warning("LLM call failed (phase %d, article %d): %s", phase, article_id, e)
        _skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        return True

    if phase == 1 and not summary:
        # LLM unavailable or returned nothing — skip temporarily
        _skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        return True

    if phase != 1 and not pairs:
        _skip_until[article_id] = time.monotonic() + _SKIP_DURATION
        return True

    async with async_session() as session:
        article = await session.get(Article, article_id)
        if not article:
            return True
        if phase == 1:
            article.ai_summary = summary
            if pairs:
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])
        else:
            article.tag_suggestions = _json.dumps([en for en, _ in pairs])
        await session.commit()

    logger.debug("Phase%d processed article %d: %s", phase, article_id, title[:50])
    return True


async def _run() -> None:
    from app.ai.llm_client import is_available

    while True:
        try:
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
