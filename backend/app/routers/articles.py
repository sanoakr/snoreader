"""Article list, detail, state update, and search endpoints."""

import math
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import Integer, case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# 保存記事の 30% 以上に付与されたタグは過剰カバレッジとみなし、Recommend スコアから除外する
_HIGH_COVERAGE_THRESHOLD = 0.3

# Recommend に含めるスコア下限（弱い 1 タグ一致を排除）
_RECOMMEND_SCORE_MIN = 1.0

from app.config import settings
from app.database import get_session
from app.models import Article, ArticleTag, Feed, Tag
from app.schemas import (
    ArticleChatRequest,
    ArticleChatResponse,
    ArticleDetail,
    ArticleOut,
    ArticleUpdate,
    ChatSource,
    ExtractActionRequest,
    MarkAllReadRequest,
    PaginatedArticles,
    TagSuggestion,
)

router = APIRouter(tags=["articles"])


@router.get("/articles", response_model=PaginatedArticles)
async def list_articles(
    feed_id: int | None = None,
    is_read: bool | None = None,
    is_saved: bool | None = None,
    tag_id: int | None = None,
    untagged: bool = False,
    sort: str = "published_at",
    order: str = "desc",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Article, Feed.title.label("feed_title")).join(Feed)
    count_stmt = select(func.count()).select_from(Article)

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)
        count_stmt = count_stmt.where(Article.feed_id == feed_id)
    if is_read is not None:
        stmt = stmt.where(Article.is_read == is_read)
        count_stmt = count_stmt.where(Article.is_read == is_read)
    if is_saved is not None:
        stmt = stmt.where(Article.is_saved == is_saved)
        count_stmt = count_stmt.where(Article.is_saved == is_saved)
    if tag_id is not None:
        stmt = stmt.where(Article.id.in_(select(ArticleTag.article_id).where(ArticleTag.tag_id == tag_id)))
        count_stmt = count_stmt.where(Article.id.in_(select(ArticleTag.article_id).where(ArticleTag.tag_id == tag_id)))
    if untagged:
        stmt = stmt.where(~Article.id.in_(select(ArticleTag.article_id)))
        count_stmt = count_stmt.where(~Article.id.in_(select(ArticleTag.article_id)))

    # Sort
    allowed_sorts = {"published_at", "fetched_at", "title"}
    sort_col = getattr(Article, sort) if sort in allowed_sorts else Article.published_at
    stmt = stmt.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
    stmt = stmt.offset(offset).limit(limit)

    total = (await session.execute(count_stmt)).scalar() or 0
    result = await session.execute(stmt)

    items = []
    for row in result:
        article = row[0]
        out = ArticleOut.model_validate(article)
        out.feed_title = row[1]
        items.append(out)

    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/articles/recommended", response_model=PaginatedArticles)
async def get_recommended_articles(
    sort: str = Query("score", pattern="^(score|date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return unread articles ranked by tag frequency overlap with saved articles."""
    import json as _json

    # Tag frequency map from saved articles: {tag_name: count}
    freq_stmt = (
        select(Tag.name, func.count(ArticleTag.article_id).label("freq"))
        .join(ArticleTag, Tag.id == ArticleTag.tag_id)
        .join(Article, ArticleTag.article_id == Article.id)
        .where(Article.is_saved == True)  # noqa: E712
        .group_by(Tag.name)
    )
    tag_freq: dict[str, int] = {
        row[0]: row[1] for row in (await session.execute(freq_stmt))
    }
    if not tag_freq:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    # Total saved articles — IDF denominator to penalize high-coverage tags
    n_saved: int = (
        await session.scalar(
            select(func.count()).select_from(Article).where(Article.is_saved == True)  # noqa: E712
        )
    ) or 1

    # カバレッジが閾値を超えるタグは除外（例: 全保存記事の 50% 超に付いているタグ）
    coverage_cutoff = n_saved * _HIGH_COVERAGE_THRESHOLD
    scoreable_freq = {t: f for t, f in tag_freq.items() if f <= coverage_cutoff}
    if not scoreable_freq:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    # Unread unsaved articles with pre-computed tag suggestions
    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .where(
            Article.is_read == False,  # noqa: E712
            Article.is_saved == False,  # noqa: E712
            Article.tag_suggestions.isnot(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    # Frequency-weighted score
    scored: list[tuple[float, Article, str]] = []
    for row in rows:
        article, feed_title = row[0], row[1]
        suggestions = set(_json.loads(article.tag_suggestions))
        score = sum(
            math.log1p(scoreable_freq[t]) * math.log(n_saved / scoreable_freq[t] + 1)
            for t in suggestions
            if t in scoreable_freq
        )
        if score > _RECOMMEND_SCORE_MIN:
            scored.append((score, article, feed_title))

    def _pub_ts(a: Article) -> float:
        try:
            return datetime.fromisoformat(a.published_at).timestamp() if a.published_at else 0.0
        except (ValueError, TypeError):
            return 0.0

    if sort == "score":
        scored.sort(key=lambda x: (-x[0] if order == "desc" else x[0], -_pub_ts(x[1])))
    else:
        scored.sort(key=lambda x: (-_pub_ts(x[1]) if order == "desc" else _pub_ts(x[1])))

    total = len(scored)
    page = scored[offset: offset + limit]

    items = []
    for score, article, feed_title in page:
        out = ArticleOut.model_validate(article)
        out.feed_title = feed_title
        out.rec_score = score
        items.append(out)
    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/articles/unrecommended", response_model=PaginatedArticles)
async def get_unrecommended_articles(
    sort: str = Query("date", pattern="^(date)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return unread articles with zero overlap with saved article tags."""
    import json as _json

    freq_stmt = (
        select(Tag.name)
        .join(ArticleTag, Tag.id == ArticleTag.tag_id)
        .join(Article, ArticleTag.article_id == Article.id)
        .where(Article.is_saved == True)  # noqa: E712
        .distinct()
    )
    saved_tag_names: set[str] = set((await session.execute(freq_stmt)).scalars())

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .where(
            Article.is_read == False,  # noqa: E712
            Article.is_saved == False,  # noqa: E712
            Article.tag_suggestions.isnot(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    def _pub_ts(a: Article) -> float:
        try:
            return datetime.fromisoformat(a.published_at).timestamp() if a.published_at else 0.0
        except (ValueError, TypeError):
            return 0.0

    unmatched: list[tuple[Article, str]] = []
    for row in rows:
        article, feed_title = row[0], row[1]
        suggestions = set(_json.loads(article.tag_suggestions))
        if not suggestions & saved_tag_names:
            unmatched.append((article, feed_title))

    unmatched.sort(key=lambda x: -_pub_ts(x[0]) if order == "desc" else _pub_ts(x[0]))

    total = len(unmatched)
    page = unmatched[offset: offset + limit]

    items = []
    for article, feed_title in page:
        out = ArticleOut.model_validate(article)
        out.feed_title = feed_title
        items.append(out)
    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/articles/extract-failed", response_model=list[ArticleOut])
async def list_extract_failed(session: AsyncSession = Depends(get_session)):
    """本文取得に失敗した記事一覧。

    `extract_status` が NULL でない記事を返す。ただし "skipped" は
    ユーザーが明示的に諦めて要約のみへ回した状態なので、ここには含めない。
    失敗種別 (not_found / forbidden / error / empty) で優先ソート。
    """
    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .where(
            Article.extract_status.isnot(None),
            Article.extract_status != "skipped",
        )
        .order_by(Article.extract_status, Article.published_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    items: list[ArticleOut] = []
    for row in rows:
        article, feed_title = row[0], row[1]
        out = ArticleOut.model_validate(article)
        out.feed_title = feed_title
        items.append(out)
    return items


@router.post("/articles/{article_id}/extract-action", response_model=dict)
async def extract_action(
    article_id: int,
    body: ExtractActionRequest,
    session: AsyncSession = Depends(get_session),
):
    """取得失敗記事に対する手動アクション: retry / skip / delete。"""
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    if body.action == "retry":
        # extract_status を NULL に戻し、Phase 0 の一時 skip もクリア。
        article.extract_status = None
        from app.services import background_processor as _bp
        _bp._extract_skip_until.pop(article_id, None)
        await session.commit()
        return {"status": "retry_queued", "article_id": article_id}

    if body.action == "skip":
        # 本文抽出は諦め、RSS summary から LLM 要約を生成する状態にする。
        article.extract_status = "skipped"
        await session.commit()
        return {"status": "skipped", "article_id": article_id}

    if body.action == "delete":
        await session.delete(article)
        await session.commit()
        return {"status": "deleted", "article_id": article_id}

    raise HTTPException(status_code=400, detail="Invalid action")


@router.get("/articles/{article_id}", response_model=ArticleDetail)
async def get_article(article_id: int, session: AsyncSession = Depends(get_session)):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    if not article.content and article.url and not article.url.startswith("snoreader://"):
        from app.services.content_extractor import extract_content
        content, _status = await extract_content(article.url)
        if content:
            article.content = content
            await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


async def _auto_attach_matching_tags(session: AsyncSession, article: Article) -> int:
    """Scan existing tags against title/body and attach any matches that aren't
    already on the article. Returns the number of tags attached."""
    from app.ai.tag_matcher import match_existing_tags

    attached_result = await session.execute(
        select(ArticleTag.tag_id).where(ArticleTag.article_id == article.id)
    )
    attached_ids = set(attached_result.scalars().all())

    all_tags: list[Tag] = list((await session.execute(select(Tag))).scalars())
    if not all_tags:
        return 0

    body_text = article.content or article.ai_summary or article.summary or ""
    # Auto-tag は付与の暴走を防ぐため 1 記事あたり 3 タグまで。
    # suggest 側は候補表示なので引き続き 10 件取得する。
    matched = match_existing_tags(all_tags, article.title, body_text)

    added = 0
    for tag in matched:
        if tag.id in attached_ids:
            continue
        session.add(ArticleTag(article_id=article.id, tag_id=tag.id))
        added += 1
        if added >= 3:
            break
    return added


@router.patch("/articles/{article_id}", response_model=ArticleOut)
async def update_article(
    article_id: int,
    body: ArticleUpdate,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    now = datetime.now(timezone.utc).isoformat()

    if body.is_read is not None:
        article.is_read = body.is_read
        article.read_at = now if body.is_read else None

    newly_saved = False
    if body.is_saved is not None:
        newly_saved = body.is_saved and not article.is_saved
        article.is_saved = body.is_saved
        article.saved_at = now if body.is_saved else None

    # 新規 Saved 化 + 未タグ付けなら既存タグで自動マッチ
    if newly_saved:
        existing_tag_ids = (await session.execute(
            select(ArticleTag.tag_id).where(ArticleTag.article_id == article.id)
        )).scalars().all()
        if not existing_tag_ids:
            await _auto_attach_matching_tags(session, article)

    await session.commit()
    await session.refresh(article)
    return ArticleOut.model_validate(article)


@router.post("/articles/auto-tag-saved", response_model=dict)
async def auto_tag_saved_articles(session: AsyncSession = Depends(get_session)):
    """Saved 記事の自動タグ付け。

    - 0 タグ: 既存タグのキーワードマッチで付与（最大 3 件）
    - 1〜3 タグ: スキップ
    - 4 タグ以上: 既存タグを全て剥がしてから再マッチ（最大 3 件） — 過去のタグ汚染を一掃する
    """
    stmt = (
        select(Article)
        .options(selectinload(Article.tags))
        .where(Article.is_saved == True)  # noqa: E712
    )
    saved_articles = (await session.execute(stmt)).scalars().all()

    processed = 0
    attached_total = 0
    for article in saved_articles:
        current_count = len(article.tags or [])
        if 1 <= current_count <= 3:
            continue
        if current_count >= 4:
            # ORM の関係代入に任せて article_tags の DELETE を生成させる。
            # 直接 delete() すると ORM の関係キャッシュと食い違い StaleDataError になる。
            article.tags = []
            await session.flush()
        added = await _auto_attach_matching_tags(session, article)
        if added:
            attached_total += added
            processed += 1
    await session.commit()
    return {"processed": processed, "attached": attached_total}


@router.post("/articles/mark-all-read", response_model=dict)
async def mark_all_read(
    body: MarkAllReadRequest,
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc).isoformat()
    stmt = select(Article).where(Article.is_read == False)  # noqa: E712
    if body.feed_id is not None:
        stmt = stmt.where(Article.feed_id == body.feed_id)

    result = await session.execute(stmt)
    articles = result.scalars().all()
    count = 0
    for article in articles:
        article.is_read = True
        article.read_at = now
        count += 1
    await session.commit()
    return {"marked": count}


@router.post("/articles/{article_id}/extract", response_model=ArticleDetail)
async def extract_article_content(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    from app.services.content_extractor import extract_content

    content, _status = await extract_content(article.url)
    if content:
        article.content = content

        from app.ai.processor import summarize_and_tag
        from app.ai.task_queue import PRIORITY_FOREGROUND
        existing = await session.execute(select(Tag.name))
        existing_names = list(existing.scalars())
        text = content or article.summary or ""
        summary, pairs = await summarize_and_tag(
            article.title, text, existing_tags=existing_names, priority=PRIORITY_FOREGROUND
        )
        if summary:
            article.ai_summary = summary
            if pairs:
                import json as _json
                article.tag_suggestions = _json.dumps([en for en, _ in pairs])

        await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.one()
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


@router.post("/articles/{article_id}/summarize", response_model=ArticleDetail)
async def summarize_article(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Generate AI summary for an article."""
    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    from app.ai.summarizer import summarize_article as _summarize
    from app.ai.task_queue import PRIORITY_FOREGROUND

    text = article.content or article.summary or ""
    summary = await _summarize(article.title, text, priority=PRIORITY_FOREGROUND)
    if summary is None:
        raise HTTPException(status_code=503, detail="LLM server unavailable")

    article.ai_summary = summary
    await session.commit()

    stmt = (
        select(Article, Feed.title.label("feed_title"))
        .join(Feed)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    result = await session.execute(stmt)
    row = result.one()
    detail = ArticleDetail.model_validate(row[0])
    detail.feed_title = row[1]
    return detail


@router.post("/articles/{article_id}/suggest-tags", response_model=list[TagSuggestion])
async def suggest_article_tags(
    article_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Return tag suggestions merged from existing-tag keyword match + LLM candidates."""
    import json as _json
    from app.ai.tag_matcher import match_existing_tags

    stmt = (
        select(Article)
        .options(selectinload(Article.tags))
        .where(Article.id == article_id)
    )
    article = (await session.execute(stmt)).scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    body_text = article.content or article.ai_summary or article.summary or ""

    all_tags: list[Tag] = list((await session.execute(select(Tag))).scalars())
    tag_by_name: dict[str, Tag] = {t.name: t for t in all_tags}

    matched = match_existing_tags(all_tags, article.title, body_text)

    llm_names: list[str] = []
    if article.tag_suggestions:
        try:
            llm_names = _json.loads(article.tag_suggestions)
        except (ValueError, TypeError):
            llm_names = []

    attached_ids = {t.id for t in (article.tags or [])}
    seen: set[str] = set()
    out: list[TagSuggestion] = []

    for t in matched:
        if t.id in attached_ids or t.name in seen:
            continue
        seen.add(t.name)
        out.append(TagSuggestion(name=t.name, name_ja=t.name_ja))

    for en in llm_names:
        if en in seen:
            continue
        seen.add(en)
        existing = tag_by_name.get(en)
        if existing and existing.id in attached_ids:
            continue
        out.append(TagSuggestion(
            name=en,
            name_ja=existing.name_ja if existing else None,
        ))

    if out:
        return out

    # Fallback: LLM で新規生成
    from app.ai.processor import summarize_and_tag
    from app.ai.task_queue import PRIORITY_FOREGROUND

    existing_names = [t.name for t in all_tags]
    _, pairs = await summarize_and_tag(
        article.title, body_text, existing_tags=existing_names, priority=PRIORITY_FOREGROUND
    )
    if not pairs:
        raise HTTPException(status_code=503, detail="LLM server unavailable or no tags generated")
    return [TagSuggestion(name=en, name_ja=ja) for en, ja in pairs]


_CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant answering questions about a specific news article. "
    "Use ONLY the provided article content as context. If the answer is not in the "
    "article, say so clearly.\n"
    "Rules:\n"
    "- Answer in conversational prose (complete sentences), NOT bullet points or lists, "
    "unless the user explicitly asks for a list.\n"
    "- Do NOT output template headers like 'SUMMARY:', 'TAGS:', or similar.\n"
    "- Do NOT start lines with '・', '-', '*', or numbered markers.\n"
    "- Answer in the user's language (Japanese if the user writes in Japanese).\n"
    "- Keep answers concise — 1-3 short paragraphs maximum."
)
_CHAT_CONTEXT_LIMIT = 4000  # 記事本文をプロンプトに埋め込む際の文字数上限
_CHAT_HISTORY_LIMIT = 10    # クライアント履歴の最大保持ターン数


@router.post("/articles/{article_id}/chat", response_model=ArticleChatResponse)
async def chat_about_article(
    article_id: int,
    body: ArticleChatRequest,
    session: AsyncSession = Depends(get_session),
):
    """Article-scoped Q&A. Optionally performs a web search when the user asks for it."""
    from app.ai.llm_client import chat_completion
    from app.ai.task_queue import PRIORITY_FOREGROUND
    from app.services import web_search

    article = await session.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # 本文ソース優先順: content > ai_summary > summary
    context_text = (article.content or article.ai_summary or article.summary or "")[:_CHAT_CONTEXT_LIMIT]

    system_content = (
        f"{_CHAT_SYSTEM_PROMPT}\n\n"
        f"Article title: {article.title}\n\n"
        f"Article content:\n{context_text}"
    )

    # Web 検索トリガー判定 → ヒットすればクエリを記事タイトル + ユーザー発言で投げる
    search_used = False
    sources: list[ChatSource] = []
    if web_search.needs_web_search(body.message):
        # 検索意図のときは記事タイトルを混ぜない（ユーザーが記事外の話題を調べたがっていることが多い）
        results = await web_search.search(body.message)
        if results:
            search_used = True
            sources = [ChatSource(title=r["title"], url=r["url"]) for r in results]
            system_content += (
                "\n\n"
                "Additional web search results (use these to supplement the article "
                "when the answer is not in the article itself). Cite sources inline by "
                "the bracketed number when you use them:\n\n"
                f"{web_search.format_results_for_llm(results)}"
            )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for m in body.history[-_CHAT_HISTORY_LIMIT:]:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": body.message})

    reply = await chat_completion(
        messages, max_tokens=512, temperature=0.3, priority=PRIORITY_FOREGROUND
    )
    if reply is None:
        raise HTTPException(status_code=503, detail="LLM server unavailable")
    return ArticleChatResponse(
        message=reply.strip(),
        search_used=search_used,
        sources=sources,
    )


_BULK_TAG_BATCH = 10  # max articles per batch


@router.post("/articles/ai-tag-saved", response_model=dict)
async def ai_tag_saved_articles(
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Run AI tag suggestions on saved articles without tags and auto-assign them (max 10 per call)."""
    stmt = select(Article).where(Article.is_saved == True).options(selectinload(Article.tags))  # noqa: E712
    result = await session.execute(stmt)
    untagged_ids = [a.id for a in result.scalars() if not a.tags]
    batch = untagged_ids[:_BULK_TAG_BATCH]
    if batch:
        background_tasks.add_task(_bulk_tag_job, batch)
    return {"queued": len(batch), "remaining": len(untagged_ids) - len(batch)}


async def _bulk_tag_job(article_ids: list[int]) -> None:
    """Background task: batch-assign AI tags to saved articles."""
    from app.ai.tagger import suggest_tags
    from app.database import async_session

    async with async_session() as session:
        # Fetch existing tags once and share across all articles
        existing_result = await session.execute(select(Tag))
        existing_names = [t.name for t in existing_result.scalars()]

        for article_id in article_ids:
            article = await session.get(Article, article_id)
            if not article:
                continue
            text = article.content or article.summary or ""
            pairs = await suggest_tags(article.title, text, existing_tags=existing_names)
            for tag_name, tag_name_ja in pairs:
                result = await session.execute(select(Tag).where(Tag.name == tag_name))
                tag = result.scalar_one_or_none()
                if not tag:
                    tag = Tag(name=tag_name, name_ja=tag_name_ja)
                    session.add(tag)
                    await session.flush()
                elif tag_name_ja and not tag.name_ja:
                    tag.name_ja = tag_name_ja
                dup = await session.execute(
                    select(ArticleTag).where(
                        ArticleTag.article_id == article_id,
                        ArticleTag.tag_id == tag.id,
                    )
                )
                if not dup.scalar_one_or_none():
                    session.add(ArticleTag(article_id=article_id, tag_id=tag.id))
            await session.commit()


@router.post("/articles/regenerate-tag-suggestions", response_model=dict)
async def regenerate_tag_suggestions(
    session: AsyncSession = Depends(get_session),
):
    """既存の tag_suggestions を NULL に戻し、background processor の Phase 2 に再生成させる。

    Issue #11: 初期プロンプトで付与された汎用タグ (ai/technology/news など) を
    修正後のプロンプトで生成し直すための管理用エンドポイント。
    """
    result = await session.execute(
        update(Article)
        .where(Article.tag_suggestions.isnot(None))
        .values(tag_suggestions=None)
    )
    await session.commit()
    return {"cleared": result.rowcount or 0}


@router.get("/ai/status")
async def ai_status(session: AsyncSession = Depends(get_session)):
    """Check LLM availability and background processing queue depth."""
    from app.ai.llm_client import is_available
    from app.ai.task_queue import queue_depth
    from app.services.background_processor import is_running

    available = await is_available()
    pending_summary = await session.scalar(
        select(func.count()).select_from(Article).where(Article.ai_summary.is_(None))
    )
    pending_tags = await session.scalar(
        select(func.count()).select_from(Article).where(
            Article.ai_summary.isnot(None), Article.tag_suggestions.is_(None)
        )
    )
    return {
        "available": available,
        "base_url": settings.llm_base_url,
        "running": is_running(),
        "queue_depth": queue_depth(),
        "pending_summary": pending_summary,
        "pending_tags": pending_tags,
    }


def _like_pattern(token: str) -> str:
    """LIKE のメタ文字 (% _ \\) をエスケープして部分一致用パターンを返す。"""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/search", response_model=PaginatedArticles)
async def search_articles(
    q: str = Query(..., min_length=1),
    feed_id: int | None = None,
    is_saved: bool | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    tokens = [t for t in q.split() if t]
    if not tokens:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    # 全文検索は articles_fts への LIKE で実装する。
    # trigram トークナイザは LIKE 検索を内部のトリグラムインデックスで高速化し、
    # かつ MATCH と異なり 2 文字以下のクエリ（「睡眠」「日本」など）や FTS5 の
    # 演算子記号 (/ + : 等) を含むクエリでも安全に動作する。
    where_clauses: list[str] = []
    params: dict[str, str] = {}
    for idx, token in enumerate(tokens):
        key = f"q{idx}"
        params[key] = _like_pattern(token)
        where_clauses.append(
            f"(title LIKE :{key} ESCAPE '\\' "
            f"OR summary LIKE :{key} ESCAPE '\\' "
            f"OR content LIKE :{key} ESCAPE '\\')"
        )
    sql = f"SELECT rowid FROM articles_fts WHERE {' AND '.join(where_clauses)}"
    fts_result = await session.execute(text(sql), params)
    matching_ids = [row[0] for row in fts_result]

    if not matching_ids:
        return PaginatedArticles(items=[], total=0, offset=offset, limit=limit)

    stmt = select(Article, Feed.title.label("feed_title")).join(Feed).where(Article.id.in_(matching_ids))
    count_stmt = select(func.count()).select_from(Article).where(Article.id.in_(matching_ids))

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)
        count_stmt = count_stmt.where(Article.feed_id == feed_id)
    if is_saved is not None:
        stmt = stmt.where(Article.is_saved == is_saved)
        count_stmt = count_stmt.where(Article.is_saved == is_saved)

    total = (await session.execute(count_stmt)).scalar() or 0
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)

    items = []
    for row in result:
        article = row[0]
        out = ArticleOut.model_validate(article)
        out.feed_title = row[1]
        items.append(out)

    return PaginatedArticles(items=items, total=total, offset=offset, limit=limit)


@router.get("/stats", response_model=list[dict])
async def get_stats(session: AsyncSession = Depends(get_session)):
    stmt = (
        select(
            Feed.id,
            Feed.title,
            func.count().label("total"),
            func.sum(case((Article.is_read == False, 1), else_=0)).label("unread"),  # noqa: E712
        )
        .join(Article, isouter=True)
        .group_by(Feed.id)
    )
    result = await session.execute(stmt)
    return [
        {"feed_id": row[0], "feed_title": row[1], "total": row[2] or 0, "unread": row[3] or 0}
        for row in result
    ]
