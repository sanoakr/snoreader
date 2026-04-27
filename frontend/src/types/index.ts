export interface Feed {
  id: number;
  url: string;
  title: string | null;
  site_url: string | null;
  description: string | null;
  favicon_url: string | null;
  fetch_interval_minutes: number;
  last_fetched_at: string | null;
  error_count: number;
  created_at: string;
  unread_count: number;
}

export interface Tag {
  id: number;
  name: string;
}

export interface Article {
  id: number;
  feed_id: number;
  guid: string;
  url: string;
  title: string;
  summary: string;
  image_url: string | null;
  author: string | null;
  published_at: string | null;
  is_read: boolean;
  is_saved: boolean;
  feed_title: string | null;
}

export interface ArticleDetail extends Article {
  content: string | null;
  fetched_at: string;
  read_at: string | null;
  saved_at: string | null;
  ai_summary: string | null;
  tags: Tag[];
}

export interface PaginatedArticles {
  items: Article[];
  total: number;
  offset: number;
  limit: number;
}

export interface ArticleFilters {
  feed_id?: number;
  is_read?: boolean;
  is_saved?: boolean;
  tag_id?: number;
  sort?: string;
  order?: string;
}
