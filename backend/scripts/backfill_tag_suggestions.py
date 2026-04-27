"""One-shot script: backfill ai_summary and tag_suggestions for all articles.

Skips articles that already have both fields populated.
Priority: Saved > Unread > Read (same as scheduler).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select

from app.ai.summarizer import summarize_article
from app.ai.tagger import suggest_tags
from app.database import async_session
from app.models import Article, Tag


async def main() -> None:
    async with async_session() as session:
        existing_names = list((await session.execute(select(Tag.name))).scalars())

        stmt = (
            select(Article)
            .where(
                or_(
                    Article.ai_summary.is_(None),
                    Article.tag_suggestions.is_(None),
                )
            )
            .order_by(
                Article.is_saved.desc(),
                Article.is_read.asc(),
                Article.published_at.desc(),
            )
        )
        articles = (await session.execute(stmt)).scalars().all()
        total = len(articles)
        print(f"Found {total} articles to process (skipping those with both fields already set)\n")

        summarized = 0
        tagged = 0

        for i, article in enumerate(articles, 1):
            prefix = f"[{i}/{total}] #{article.id} {article.title[:45]}"
            changed = False

            # Step 1: summarize if missing
            if not article.ai_summary:
                try:
                    text = article.content or article.summary or ""
                    summary = await summarize_article(article.title, text)
                    if summary:
                        article.ai_summary = summary
                        summarized += 1
                        changed = True
                        print(f"{prefix}\n  → summary generated")
                    else:
                        print(f"{prefix}\n  → summary: LLM returned empty")
                except Exception as e:
                    print(f"{prefix}\n  → summary ERROR: {e}")

            # Step 2: suggest tags if missing
            if not article.tag_suggestions:
                try:
                    text = article.ai_summary or article.content or article.summary or ""
                    pairs = await suggest_tags(article.title, text, existing_tags=existing_names)
                    if pairs:
                        article.tag_suggestions = json.dumps([en for en, _ in pairs])
                        tags_str = ", ".join(en for en, _ in pairs)
                        tagged += 1
                        changed = True
                        print(f"{prefix}\n  → tags: {tags_str}")
                    else:
                        print(f"{prefix}\n  → tags: (none generated)")
                except Exception as e:
                    print(f"{prefix}\n  → tags ERROR: {e}")

            if changed:
                await session.commit()

        print(f"\nDone. summarized={summarized}  tagged={tagged}  total_processed={total}")


asyncio.run(main())
