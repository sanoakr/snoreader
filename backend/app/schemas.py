"""Pydantic schemas for API request/response."""

from pydantic import BaseModel, HttpUrl


# --- Tag ---

class TagOut(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class TagCreate(BaseModel):
    name: str


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

    model_config = {"from_attributes": True}


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


class MarkAllReadRequest(BaseModel):
    feed_id: int | None = None


# --- Pagination ---

class PaginatedArticles(BaseModel):
    items: list[ArticleOut]
    total: int
    offset: int
    limit: int
