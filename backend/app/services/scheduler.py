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
    """未要約記事をバッチ処理する低優先度バックグラウンドジョブ。"""
    if _summarize_lock.locked():
        logger.debug("Summarize job skipped: previous run still active")
        return

    from app.ai.llm_client import is_available
    if not await is_available():
        return

    async with _summarize_lock:
        from app.ai.summarizer import summarize_article
        from app.database import async_session
        from app.models import Article

        async with async_session() as session:
            # Fetch unsummarized articles ordered by priority: Saved > Unread > Read
            stmt = (
                select(Article)
                .where(Article.ai_summary.is_(None))
                .order_by(
                    Article.is_saved.desc(),
                    Article.is_read.asc(),
                    Article.published_at.desc(),
                )
                .limit(settings.summarize_batch_size)
            )
            result = await session.execute(stmt)
            articles = result.scalars().all()

            if not articles:
                return

            logger.info("Background summarize: processing %d articles", len(articles))
            for article in articles:
                try:
                    text = article.content or article.summary or ""
                    summary = await summarize_article(article.title, text)
                    if summary:
                        article.ai_summary = summary
                        await session.commit()
                        logger.debug("Summarized article %d: %s", article.id, article.title[:40])
                except Exception as e:
                    logger.warning("Failed to summarize article %d: %s", article.id, e)


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
