"""Background scheduler for periodic feed fetching and AI summarization."""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.services.feed_fetcher import fetch_all_feeds

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_summarize_lock = asyncio.Lock()


async def _fetch_job():
    await fetch_all_feeds()


async def _summarize_job():
    """Low-priority background job: summarize articles and compute tag suggestions."""
    if _summarize_lock.locked():
        logger.debug("Summarize job skipped: previous run still active")
        return

    from app.ai.llm_client import is_available
    if not await is_available():
        return

    async with _summarize_lock:
        import json as _json

        from app.ai.summarizer import summarize_article
        from app.ai.tagger import suggest_tags as _suggest_tags
        from app.database import async_session
        from app.models import Article, Tag

        async with async_session() as session:
            existing_names = list((await session.execute(select(Tag.name))).scalars())

            # Phase 1: summarize unsummarized articles + compute tag suggestions
            stmt1 = (
                select(Article)
                .where(Article.ai_summary.is_(None))
                .order_by(
                    Article.is_saved.desc(),
                    Article.is_read.asc(),
                    Article.published_at.desc(),
                )
                .limit(settings.summarize_batch_size)
            )
            articles = (await session.execute(stmt1)).scalars().all()
            if articles:
                logger.info("Background summarize (phase1): processing %d articles", len(articles))
            for article in articles:
                try:
                    text = article.content or article.summary or ""
                    summary = await summarize_article(article.title, text)
                    if summary:
                        article.ai_summary = summary
                        pairs = await _suggest_tags(article.title, summary, existing_tags=existing_names)
                        if pairs:
                            article.tag_suggestions = _json.dumps([en for en, _ in pairs])
                        await session.commit()
                        logger.debug("Summarized article %d: %s", article.id, article.title[:40])
                except Exception as e:
                    logger.warning("Failed to summarize article %d: %s", article.id, e)

            # Phase 2: backfill tag_suggestions for already-summarized articles
            stmt2 = (
                select(Article)
                .where(Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None))
                .order_by(Article.is_saved.desc(), Article.published_at.desc())
                .limit(settings.summarize_batch_size)
            )
            backfill = (await session.execute(stmt2)).scalars().all()
            if backfill:
                logger.info("Background summarize (phase2 backfill): processing %d articles", len(backfill))
            for article in backfill:
                try:
                    pairs = await _suggest_tags(article.title, article.ai_summary, existing_tags=existing_names)
                    if pairs:
                        article.tag_suggestions = _json.dumps([en for en, _ in pairs])
                        await session.commit()
                except Exception as e:
                    logger.warning("Failed to suggest tags for article %d: %s", article.id, e)

            # Phase 3: tag suggestions for unread unsaved articles (recommendation candidates)
            # Uses content/summary directly — no ai_summary required
            stmt3 = (
                select(Article)
                .where(
                    Article.is_read == False,  # noqa: E712
                    Article.is_saved == False,  # noqa: E712
                    Article.tag_suggestions.is_(None),
                )
                .order_by(Article.published_at.desc())
                .limit(settings.summarize_batch_size)
            )
            candidates = (await session.execute(stmt3)).scalars().all()
            if candidates:
                logger.info("Background summarize (phase3 rec candidates): processing %d articles", len(candidates))
            for article in candidates:
                try:
                    text = article.ai_summary or article.content or article.summary or ""
                    pairs = await _suggest_tags(article.title, text, existing_tags=existing_names)
                    if pairs:
                        article.tag_suggestions = _json.dumps([en for en, _ in pairs])
                        await session.commit()
                except Exception as e:
                    logger.warning("Failed to suggest tags (phase3) for article %d: %s", article.id, e)


def summarize_job_running() -> bool:
    return _summarize_lock.locked()


def start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _fetch_job,
        "interval",
        minutes=settings.feed_fetch_interval_minutes,
        id="fetch_all_feeds",
        replace_existing=True,
    )
    _scheduler.add_job(
        _summarize_job,
        "interval",
        seconds=settings.summarize_interval_seconds,
        id="background_summarize",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started (fetch=%d min, summarize=%ds batch=%d)",
        settings.feed_fetch_interval_minutes,
        settings.summarize_interval_seconds,
        settings.summarize_batch_size,
    )


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
