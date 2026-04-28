"""Background scheduler for periodic feed fetching."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.services.feed_fetcher import fetch_all_feeds

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _fetch_job():
    await fetch_all_feeds()


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
    _scheduler.start()
    logger.info("Scheduler started (fetch=%d min)", settings.feed_fetch_interval_minutes)


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
