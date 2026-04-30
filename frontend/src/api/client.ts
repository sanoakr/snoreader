import type { Article, ArticleDetail, ArticleFilters, ChatMessage, ChatResponse, Feed, PaginatedArticles, Tag, TagSuggestion } from '../types';

const BASE = '/api';

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// --- Feeds ---

export function getFeeds(): Promise<Feed[]> {
  return fetchJSON(`${BASE}/feeds`);
}

export function createFeed(url: string): Promise<Feed> {
  return fetchJSON(`${BASE}/feeds`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
}

export function deleteFeed(id: number): Promise<void> {
  return fetchJSON(`${BASE}/feeds/${id}`, { method: 'DELETE' });
}

export function refreshFeed(id: number): Promise<{ new_articles: number }> {
  return fetchJSON(`${BASE}/feeds/${id}/refresh`, { method: 'POST' });
}

// --- Articles ---

export function getArticles(
  filters: ArticleFilters = {},
  offset = 0,
  limit = 50,
): Promise<PaginatedArticles> {
  if (filters.recommended) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters.sort) params.set('sort', filters.sort);
    if (filters.order) params.set('order', filters.order);
    return fetchJSON(`${BASE}/articles/recommended?${params}`);
  }
  if (filters.unrecommended) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters.order) params.set('order', filters.order);
    return fetchJSON(`${BASE}/articles/unrecommended?${params}`);
  }
  const params = new URLSearchParams();
  if (filters.feed_id != null) params.set('feed_id', String(filters.feed_id));
  if (filters.is_read != null) params.set('is_read', String(filters.is_read));
  if (filters.is_saved != null) params.set('is_saved', String(filters.is_saved));
  if (filters.tag_id != null) params.set('tag_id', String(filters.tag_id));
  if (filters.sort) params.set('sort', filters.sort);
  if (filters.order) params.set('order', filters.order);
  params.set('offset', String(offset));
  params.set('limit', String(limit));
  return fetchJSON(`${BASE}/articles?${params}`);
}

export function getArticle(id: number): Promise<ArticleDetail> {
  return fetchJSON(`${BASE}/articles/${id}`);
}

export function updateArticle(id: number, data: { is_read?: boolean; is_saved?: boolean }): Promise<Article> {
  return fetchJSON(`${BASE}/articles/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export function markAllRead(feedId?: number): Promise<{ marked: number }> {
  return fetchJSON(`${BASE}/articles/mark-all-read`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ feed_id: feedId ?? null }),
  });
}


// --- AI ---

export function summarizeArticle(id: number): Promise<ArticleDetail> {
  return fetchJSON(`${BASE}/articles/${id}/summarize`, { method: 'POST' });
}

export function extractArticleContent(id: number): Promise<ArticleDetail> {
  return fetchJSON(`${BASE}/articles/${id}/extract`, { method: 'POST' });
}

export function suggestTags(id: number): Promise<TagSuggestion[]> {
  return fetchJSON(`${BASE}/articles/${id}/suggest-tags`, { method: 'POST' });
}

export function chatWithArticle(
  id: number,
  message: string,
  history: ChatMessage[],
): Promise<ChatResponse> {
  return fetchJSON(`${BASE}/articles/${id}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history }),
  });
}

export interface AiStatus {
  available: boolean;
  base_url: string;
  running: boolean;
  pending_summary: number;
  pending_tags: number;
}

export function getAiStatus(): Promise<AiStatus> {
  return fetchJSON(`${BASE}/ai/status`);
}

export function aiTagSaved(): Promise<{ queued: number; remaining: number }> {
  return fetchJSON(`${BASE}/articles/ai-tag-saved`, { method: 'POST' });
}

export function fillTagTranslations(): Promise<{ translating?: number; translated?: number }> {
  return fetchJSON(`${BASE}/tags/fill-translations`, { method: 'POST' });
}

// --- Search ---

export function searchArticles(
  q: string,
  filters: { feed_id?: number; is_saved?: boolean } = {},
  offset = 0,
  limit = 50,
): Promise<PaginatedArticles> {
  const params = new URLSearchParams({ q });
  if (filters.feed_id != null) params.set('feed_id', String(filters.feed_id));
  if (filters.is_saved != null) params.set('is_saved', String(filters.is_saved));
  params.set('offset', String(offset));
  params.set('limit', String(limit));
  return fetchJSON(`${BASE}/search?${params}`);
}

// --- Tags ---

export function getTags(): Promise<Tag[]> {
  return fetchJSON(`${BASE}/tags`);
}

export function renameTag(id: number, name: string): Promise<Tag> {
  return fetchJSON(`${BASE}/tags/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

export function bulkDeleteTags(tag_ids: number[]): Promise<{ deleted: number }> {
  return fetchJSON(`${BASE}/tags/bulk`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag_ids }),
  });
}

export function addTagToArticle(articleId: number, name: string, name_ja?: string | null): Promise<Tag> {
  return fetchJSON(`${BASE}/articles/${articleId}/tags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, name_ja: name_ja ?? null }),
  });
}

export function removeTagFromArticle(articleId: number, tagId: number): Promise<void> {
  return fetchJSON(`${BASE}/articles/${articleId}/tags/${tagId}`, { method: 'DELETE' });
}

// --- OPML ---

export function importOpml(file: File): Promise<{ created: number; skipped: number; total: number }> {
  const formData = new FormData();
  formData.append('file', file);
  return fetchJSON(`${BASE}/opml/import`, { method: 'POST', body: formData });
}

export const opmlExportUrl = `${BASE}/opml/export`;

// --- Import ---

export interface ImportResult {
  articles_created: number;
  articles_skipped: number;
  feeds_created: number;
  total: number;
}

export function importArticles(file: File): Promise<ImportResult> {
  const formData = new FormData();
  formData.append('file', file);
  return fetchJSON(`${BASE}/import/articles`, { method: 'POST', body: formData });
}
