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
