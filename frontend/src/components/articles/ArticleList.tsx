import { useCallback, useEffect, useRef, useState } from 'react';
import { useArticles, useMarkAllRead, useSearchArticles, useUpdateArticle } from '../../hooks/useArticles';
import type { Article, ArticleFilters } from '../../types';
import { ArticleCard } from './ArticleCard';
import { ArticleReader } from './ArticleReader';

interface Props {
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
}

const LIMIT = 50;

export function ArticleList({ filters, onFilterChange }: Props) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [pages, setPages] = useState(1);
  const searchRef = useRef<HTMLInputElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Fetch multiple pages for infinite scroll
  const pageQueries = Array.from({ length: pages }, (_, i) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useArticles(filters, i * LIMIT, LIMIT)
  );
  const searchResults = useSearchArticles(searchQuery, { feed_id: filters.feed_id, is_saved: filters.is_saved });
  const markAllRead = useMarkAllRead();
  const updateArticle = useUpdateArticle();

  const isSearching = searchQuery.length > 0;
  const firstPageData = pageQueries[0]?.data;
  const total = isSearching ? (searchResults.data?.total ?? 0) : (firstPageData?.total ?? 0);
  const isLoading = isSearching ? searchResults.isLoading : pageQueries[0]?.isLoading;

  const articles: Article[] = isSearching
    ? (searchResults.data?.items ?? [])
    : pageQueries.flatMap(q => q.data?.items ?? []);

  const hasMore = !isSearching && articles.length < total;

  // Reset pages on filter/search change
  useEffect(() => {
    setPages(1);
    listRef.current?.scrollTo(0, 0);
  }, [filters, searchQuery]);

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    if (!sentinelRef.current || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) setPages(p => p + 1);
      },
      { rootMargin: '200px' }
    );
    observer.observe(sentinelRef.current);
    return () => observer.disconnect();
  }, [hasMore, articles.length]);

  // Keyboard navigation
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) return;

    const currentIndex = articles.findIndex(a => a.id === selectedId);

    switch (e.key) {
      case 'j':
      case 'ArrowDown': {
        e.preventDefault();
        const next = currentIndex < articles.length - 1 ? currentIndex + 1 : currentIndex;
        if (next === -1 && articles.length > 0) {
          setSelectedId(articles[0].id);
        } else if (articles[next]) {
          setSelectedId(articles[next].id);
        }
        break;
      }
      case 'k':
      case 'ArrowUp': {
        e.preventDefault();
        const prev = currentIndex > 0 ? currentIndex - 1 : 0;
        if (articles[prev]) setSelectedId(articles[prev].id);
        break;
      }
      case 's': {
        if (selectedId != null) {
          const article = articles.find(a => a.id === selectedId);
          if (article) updateArticle.mutate({ id: article.id, data: { is_saved: !article.is_saved } });
        }
        break;
      }
      case 'o':
      case 'Enter': {
        if (selectedId != null) {
          const article = articles.find(a => a.id === selectedId);
          if (article) window.open(article.url, '_blank');
        }
        break;
      }
      case '/': {
        e.preventDefault();
        searchRef.current?.focus();
        break;
      }
    }
  }, [articles, selectedId, updateArticle]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  useEffect(() => {
    if (selectedId != null) {
      const el = document.querySelector(`[data-article-id="${selectedId}"]`);
      el?.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedId]);

  const filterBtnClass = (active: boolean) =>
    `px-2 py-1 text-xs rounded ${active ? 'bg-gray-800 text-white dark:bg-gray-200 dark:text-gray-900' : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700'}`;

  return (
    <div className="flex flex-1 min-w-0">
      {/* Article list panel */}
      <div className="w-full md:w-96 shrink-0 md:border-r border-gray-200 dark:border-gray-700 flex flex-col h-screen">
        {/* Toolbar */}
        <div className="p-2 border-b border-gray-200 dark:border-gray-700 space-y-2">
          <input
            ref={searchRef}
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search articles... ( / )"
            className="w-full px-2 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
          />
          <div className="flex items-center gap-1.5 flex-wrap">
            <button onClick={() => onFilterChange({ ...filters, is_read: undefined })} className={filterBtnClass(filters.is_read === undefined)}>All</button>
            <button onClick={() => onFilterChange({ ...filters, is_read: false })} className={filterBtnClass(filters.is_read === false)}>Unread</button>
            <button onClick={() => onFilterChange({ ...filters, is_read: true })} className={filterBtnClass(filters.is_read === true)}>Read</button>
            <div className="flex-1" />
            <span className="text-xs text-gray-400">{total}</span>
            <button onClick={() => markAllRead.mutate(filters.feed_id)} disabled={markAllRead.isPending} className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50">
              Mark all read
            </button>
          </div>
        </div>

        {/* Article list */}
        <div ref={listRef} className="flex-1 overflow-y-auto">
          {isLoading && <p className="p-4 text-sm text-gray-400">Loading...</p>}
          {!isLoading && articles.length === 0 && (
            <p className="p-4 text-sm text-gray-400">No articles found</p>
          )}
          {articles.map((article) => (
            <ArticleCard
              key={article.id}
              article={article}
              isSelected={article.id === selectedId}
              onClick={() => setSelectedId(article.id)}
            />
          ))}
          {hasMore && <div ref={sentinelRef} className="p-3 text-center text-xs text-gray-400">Loading more...</div>}
        </div>
      </div>

      {/* Reader panel */}
      <div className={`flex-1 min-w-0 ${selectedId ? 'fixed inset-0 z-20 bg-white dark:bg-gray-950 md:relative md:z-auto' : 'hidden md:block'}`}>
        {selectedId ? (
          <>
            <button
              onClick={() => setSelectedId(null)}
              className="md:hidden fixed top-2 left-2 z-30 p-1.5 bg-gray-100 dark:bg-gray-800 rounded"
            >
              ← Back
            </button>
            <ArticleReader articleId={selectedId} />
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-gray-400 text-sm">
            <div className="text-center space-y-2">
              <p>Select an article to read</p>
              <p className="text-xs text-gray-500">
                <kbd className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-[10px]">j</kbd>
                <kbd className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-[10px] ml-1">k</kbd>
                {' '}navigate{' '}
                <kbd className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-[10px]">s</kbd>
                {' '}save{' '}
                <kbd className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-[10px]">o</kbd>
                {' '}open{' '}
                <kbd className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded text-[10px]">/</kbd>
                {' '}search
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
