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
  name_ja: string | null;
}

export interface TagSuggestion {
  name: string;
  name_ja: string | null;
  existing?: boolean;
}

export interface ExcludePattern {
  id: number;
  pattern: string;
  created_at: string;
  purged: number;
}

export interface GenreCount {
  genre: string;
  label_ja: string;
  /** direct_count + 子の合計 */
  unread_count: number;
  /** そのキーが直接付いている記事数（子ルールがまだ無いタグの記事） */
  direct_count: number;
  children: GenreCount[];
}

export interface GenreRuleDef {
  id: number;
  tag: string;
}

export interface GenreDef {
  id: number;
  key: string;
  label_ja: string;
  priority: number;
  parent_id: number | null;
  rules: GenreRuleDef[];
  generic_rules: GenreRuleDef[];
  reclassified: number;
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
  rec_score?: number;
  extract_status?: string | null;
  dismissed_at: string | null;
}

export type ExtractAction = 'retry' | 'skip' | 'delete';

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
  untagged?: boolean;
  sort?: string;
  order?: string;
  recommended?: boolean;
  unrecommended?: boolean;
  extract_failed?: boolean;
  genre?: string;
  genre_exact?: boolean;
  dismissed?: boolean;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatSource {
  title: string;
  url: string;
}

export interface ChatResponse {
  message: string;
  search_used: boolean;
  sources: ChatSource[];
}

export interface ChatSuggestionsResponse {
  questions: string[];
  /** この応答が LLM 生成によるものか（キャッシュ返却なら false） */
  generated: boolean;
}

export interface ProposedChild {
  key: string;
  label_ja: string;
  tags: string[];
  estimated_unread: number;
}

// 未読が上限を超えた葉ジャンルの分割案。件数はバックエンドで実際に分類し直した実測値
export interface SplitSuggestion {
  id: number;
  genre_key: string;
  strategy: string;
  before: number;
  projected_max: number;
  children: ProposedChild[];
  demote_tags: string[];
  created_at: string;
  limit: number;
}
