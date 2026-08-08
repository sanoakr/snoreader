"""Pydantic schemas for API request/response."""

from typing import Literal

from pydantic import BaseModel, HttpUrl


# --- Tag ---

class TagOut(BaseModel):
    id: int
    name: str
    name_ja: str | None = None

    model_config = {"from_attributes": True}


class TagCreate(BaseModel):
    name: str
    name_ja: str | None = None


class TagSuggestion(BaseModel):
    name: str
    name_ja: str | None = None


class TagUpdate(BaseModel):
    name: str


class BulkDeleteTagsRequest(BaseModel):
    tag_ids: list[int]


# --- Feed ---

class FeedCreate(BaseModel):
    url: HttpUrl


class FeedUpdate(BaseModel):
    title: str | None = None
    fetch_interval_minutes: int | None = None


class FeedOut(BaseModel):
    id: int
    url: str
    title: str | None
    site_url: str | None
    description: str | None
    favicon_url: str | None
    fetch_interval_minutes: int
    last_fetched_at: str | None
    error_count: int
    created_at: str
    unread_count: int = 0

    model_config = {"from_attributes": True}


# --- Article ---

class ArticleOut(BaseModel):
    id: int
    feed_id: int
    guid: str
    url: str
    title: str
    summary: str
    image_url: str | None
    author: str | None
    published_at: str | None
    is_read: bool
    is_saved: bool
    feed_title: str | None = None
    rec_score: float | None = None
    extract_status: str | None = None
    dismissed_at: str | None = None

    model_config = {"from_attributes": True}


class ExtractActionRequest(BaseModel):
    action: Literal["retry", "skip", "delete"]


class ArticleDetail(ArticleOut):
    content: str | None
    fetched_at: str
    read_at: str | None
    saved_at: str | None
    ai_summary: str | None
    tags: list[TagOut] = []


class ArticleUpdate(BaseModel):
    is_read: bool | None = None
    is_saved: bool | None = None
    auto_tag: bool = True  # False にすると新規 Saved 時の自動タグ付けをスキップ


class MarkAllReadRequest(BaseModel):
    feed_id: int | None = None
    genre: str | None = None


class DismissRequest(BaseModel):
    genre: str | None = None
    ids: list[int] | None = None


class DedupRequest(BaseModel):
    dry_run: bool = False


class DedupResponse(BaseModel):
    duplicate_groups: int
    deleted: int
    dry_run: bool


# --- Exclude patterns ---

class ExcludePatternCreate(BaseModel):
    pattern: str


class ExcludePatternOut(BaseModel):
    id: int
    pattern: str
    created_at: str
    purged: int = 0

    model_config = {"from_attributes": True}


class GenreCountOut(BaseModel):
    genre: str
    label_ja: str
    unread_count: int


class GenreRuleOut(BaseModel):
    id: int
    tag: str


class GenreOut(BaseModel):
    id: int
    key: str
    label_ja: str
    priority: int
    # 管理 UI がチップの削除に rule id を使うので、タグ名だけでなく id も返す
    rules: list[GenreRuleOut] = []
    generic_rules: list[GenreRuleOut] = []
    # 変更系エンドポイントは全件再分類した件数をここに詰める（作成直後は 0）
    reclassified: int = 0


class GenreCreate(BaseModel):
    key: str
    label_ja: str
    priority: int = 100


class GenreUpdate(BaseModel):
    label_ja: str | None = None
    priority: int | None = None


class GenreRuleCreate(BaseModel):
    tag: str
    genre_id: int
    is_generic: bool = False


class ReclassifyResult(BaseModel):
    reclassified: int


# --- Pagination ---

class PaginatedArticles(BaseModel):
    items: list[ArticleOut]
    total: int
    offset: int
    limit: int


# --- Chat ---

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ArticleChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ChatSource(BaseModel):
    title: str
    url: str


class ArticleChatResponse(BaseModel):
    message: str
    search_used: bool = False
    sources: list[ChatSource] = []
