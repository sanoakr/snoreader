import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useAiStatus, useArticles, useMarkAllRead, useSearchArticles, useUpdateArticle } from '../../hooks/useArticles';
import { useTags, useBulkDeleteTags } from '../../hooks/useTags';
import type { Article, ArticleFilters } from '../../types';
import { ArticleCard } from './ArticleCard';
import { ArticleReader } from './ArticleReader';
import { Spinner } from '../common/Spinner';

interface Props {
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
  tagLang: 'en' | 'ja';
}

export function ArticleList({ filters, onFilterChange, tagLang }: Props) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Keeps the selected article visible across background refetches (e.g. in Unread view)
  const pinnedArticleRef = useRef<Article | null>(null);

  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading: isArticlesLoading,
  } = useArticles(filters);

  const searchResults = useSearchArticles(searchQuery, { feed_id: filters.feed_id, is_saved: filters.is_saved });
  const markAllRead = useMarkAllRead();
  const updateArticle = useUpdateArticle();
  const { data: aiStatus } = useAiStatus();
  const aiAvailable = aiStatus?.available ?? false;
  const { data: tags } = useTags();
  const bulkDeleteTags = useBulkDeleteTags();

  const selectedTag = filters.tag_id != null ? tags?.find(t => t.id === filters.tag_id) : null;

  const isSearching = searchQuery.length > 0;
  const isLoading = isSearching ? searchResults.isLoading : isArticlesLoading;

  const articles: Article[] = isSearching
    ? (searchResults.data?.items ?? [])
    : (data?.pages.flatMap(p => p.items) ?? []);

  const total = isSearching
    ? (searchResults.data?.total ?? 0)
    : (data?.pages[0]?.total ?? 0);

  // When a background refetch removes the selected article (e.g. Unread view after mark-as-read),
  // keep it visible by prepending the pinned copy.
  const displayArticles = useMemo(() => {
    const pinned = pinnedArticleRef.current;
    if (!pinned || !selectedId || pinned.id !== selectedId) return articles;
    const freshVersion = articles.find(a => a.id === pinned.id);
    if (freshVersion) {
      // Update pin with fresh data for next time
      pinnedArticleRef.current = freshVersion;
      return articles;
    }
    return [pinned, ...articles];
  }, [articles, selectedId]);

  // Reset scroll on filter/search change, clear pinned article, and auto-select first article
  useEffect(() => {
    listRef.current?.scrollTo(0, 0);
    pinnedArticleRef.current = null;
    setSelectedId(null);
  }, [filters, searchQuery]);

  // Auto-select the first article when the article list loads after a filter change
  useEffect(() => {
    if (selectedId != null) return;
    if (isLoading) return;
    if (displayArticles.length > 0) {
      setSelectedId(displayArticles[0].id);
    }
  }, [displayArticles, isLoading, selectedId]);

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    if (!sentinelRef.current || !hasNextPage || isSearching) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !isFetchingNextPage) fetchNextPage();
      },
      { rootMargin: '200px' }
    );
    observer.observe(sentinelRef.current);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, isSearching]);

  const currentIndex = displayArticles.findIndex(a => a.id === selectedId);
  const hasPrev = currentIndex > 0;
  const hasNext = currentIndex >= 0 && currentIndex < displayArticles.length - 1;

  const goNext = useCallback((idx: number) => {
    const next = idx < displayArticles.length - 1 ? idx + 1 : idx;
    if (next === -1 && displayArticles.length > 0) setSelectedId(displayArticles[0].id);
    else if (displayArticles[next]) setSelectedId(displayArticles[next].id);
  }, [displayArticles]);

  const goPrev = useCallback((idx: number) => {
    const prev = idx > 0 ? idx - 1 : 0;
    if (displayArticles[prev]) setSelectedId(displayArticles[prev].id);
  }, [displayArticles]);

  // Keyboard navigation
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable) return;

    const currentIndex = displayArticles.findIndex(a => a.id === selectedId);

    switch (e.key) {
      case 'j':
      case 'ArrowDown': {
        e.preventDefault();
        goNext(currentIndex);
        break;
      }
      case 'k':
      case 'ArrowUp': {
        e.preventDefault();
        goPrev(currentIndex);
        break;
      }
      case 's': {
        if (selectedId != null) {
          const article = displayArticles.find(a => a.id === selectedId);
          if (article) updateArticle.mutate({ id: article.id, data: { is_saved: !article.is_saved } });
        }
        break;
      }
      case 'o':
      case 'Enter': {
        if (selectedId != null) {
          const article = displayArticles.find(a => a.id === selectedId);
          if (article) window.open(article.url, '_blank');
        }
        break;
      }
      case '/': {
        e.preventDefault();
        searchRef.current?.focus();
        break;
      }
      case 'r': {
        e.preventDefault();
        queryClient.refetchQueries({ queryKey: ['articles'] });
        queryClient.refetchQueries({ queryKey: ['feeds'] });
        break;
      }
    }
  }, [displayArticles, selectedId, updateArticle, queryClient]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  useEffect(() => {
    if (selectedId != null) {
      // On mobile the reader overlay covers the list; skip scrollIntoView to
      // avoid iOS Safari triggering unintended window scroll through the overlay
      if (window.innerWidth < 768) return;
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
            {!filters.recommended && !filters.unrecommended && !filters.is_saved && (
              <>
                <button onClick={() => onFilterChange({ ...filters, is_read: false })} className={filterBtnClass(filters.is_read === false)}>Unread</button>
                <button onClick={() => onFilterChange({ ...filters, is_read: undefined })} className={filterBtnClass(filters.is_read === undefined)}>All</button>
                <button onClick={() => onFilterChange({ ...filters, is_read: true })} className={filterBtnClass(filters.is_read === true)}>Read</button>
              </>
            )}
            {filters.recommended && (
              <>
                <button onClick={() => onFilterChange({ ...filters, sort: undefined, order: undefined })} className={filterBtnClass(!filters.sort || filters.sort === 'score')}>Score</button>
                <button onClick={() => onFilterChange({ ...filters, sort: 'date', order: 'desc' })} className={filterBtnClass(filters.sort === 'date')}>Date</button>
              </>
            )}
            {filters.unrecommended && (
              <>
                <button onClick={() => onFilterChange({ ...filters, sort: 'date', order: 'desc' })} className={filterBtnClass(!filters.order || filters.order === 'desc')}>New</button>
                <button onClick={() => onFilterChange({ ...filters, sort: 'date', order: 'asc' })} className={filterBtnClass(filters.order === 'asc')}>Old</button>
              </>
            )}
            <div className="flex-1" />
            <span className="text-xs text-gray-400">{total}</span>
            {selectedTag ? (
              <button
                onClick={() => {
                  if (!confirm(`タグ「${selectedTag.name}」を削除しますか？`)) return;
                  bulkDeleteTags.mutate([selectedTag.id], {
                    onSuccess: () => onFilterChange({ ...filters, tag_id: undefined }),
                  });
                }}
                disabled={bulkDeleteTags.isPending}
                className="text-xs text-red-400 hover:text-red-600 disabled:opacity-50"
              >
                #{selectedTag.name} を削除
              </button>
            ) : !filters.is_saved && (
              <button onClick={() => markAllRead.mutate(filters.feed_id)} disabled={markAllRead.isPending} className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50">
                Mark all read
              </button>
            )}
          </div>
        </div>

        {/* Article list */}
        <div ref={listRef} className="flex-1 overflow-y-auto">
          {isLoading && <div className="flex justify-center p-6"><Spinner /></div>}
          {!isLoading && articles.length === 0 && (
            <p className="p-4 text-sm text-gray-400">No articles found</p>
          )}
          {displayArticles.map((article) => (
            <ArticleCard
              key={article.id}
              article={article}
              isSelected={article.id === selectedId}
              onClick={() => {
                pinnedArticleRef.current = article;
                setSelectedId(article.id);
              }}
              dimRead={!filters.is_saved}
            />
          ))}
          {(hasNextPage && !isSearching) && (
            <div ref={sentinelRef} className="flex justify-center p-3">
              {isFetchingNextPage ? <Spinner size="sm" /> : <span className="text-xs text-gray-400">Scroll for more</span>}
            </div>
          )}
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
            <ArticleReader
              key={selectedId}
              articleId={selectedId}
              tagLang={tagLang}
              aiAvailable={aiAvailable}
              onPrev={hasPrev ? () => goPrev(currentIndex) : undefined}
              onNext={hasNext ? () => goNext(currentIndex) : undefined}
            />
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
