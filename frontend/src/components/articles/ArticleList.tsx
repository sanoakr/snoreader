import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useAiStatus, useArticles, useExtractAction, useExtractFailed, useMarkAllRead, useSearchArticles, useUpdateArticle } from '../../hooks/useArticles';
import { useTags, useBulkDeleteTags } from '../../hooks/useTags';
import type { Article, ArticleFilters } from '../../types';
import { ArticleCard } from './ArticleCard';
import { ArticleReader } from './ArticleReader';
import { Spinner } from '../common/Spinner';

const EXTRACT_STATUS_LABEL: Record<string, string> = {
  not_found: '404',
  forbidden: '403',
  error: 'error',
  empty: '本文空',
  skipped: 'skipped',
};

const EXTRACT_STATUS_COLOR: Record<string, string> = {
  not_found: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  forbidden: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
  error: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  empty: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  skipped: 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

function ExtractStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  const label = EXTRACT_STATUS_LABEL[status] ?? status;
  const color = EXTRACT_STATUS_COLOR[status] ?? EXTRACT_STATUS_COLOR.error;
  return <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${color}`}>{label}</span>;
}

interface Props {
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
  tagLang: 'en' | 'ja';
  onTotalChange?: (total: number) => void;
}

export function ArticleList({ filters, onFilterChange, tagLang, onTotalChange }: Props) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  // Keeps the selected article visible across background refetches (e.g. in Unread view)
  const pinnedArticleRef = useRef<Article | null>(null);

  const isExtractFailedView = !!filters.extract_failed;
  const isUnreadView = filters.is_read === false && !isExtractFailedView;
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading: isArticlesLoading,
    refetch: refetchArticles,
  } = useArticles(
    isExtractFailedView ? { ...filters, extract_failed: undefined } : filters,
    isUnreadView,
  );

  const PULL_THRESHOLD = 60;
  const pullStartYRef = useRef<number | null>(null);
  const [isPulling, setIsPulling] = useState(false);
  const [pullDistance, setPullDistance] = useState(0);

  const handleListTouchStart = (e: React.TouchEvent) => {
    if ((listRef.current?.scrollTop ?? 0) === 0) {
      pullStartYRef.current = e.touches[0].clientY;
    }
  };
  const handleListTouchMove = (e: React.TouchEvent) => {
    if (pullStartYRef.current === null || isPulling) return;
    const dy = e.touches[0].clientY - pullStartYRef.current;
    setPullDistance(dy > 0 ? Math.min(dy, PULL_THRESHOLD * 1.5) : 0);
  };
  const handleListTouchEnd = (e: React.TouchEvent) => {
    const dy = pullStartYRef.current !== null
      ? e.changedTouches[0].clientY - pullStartYRef.current
      : 0;
    pullStartYRef.current = null;
    setPullDistance(0);
    if (dy >= PULL_THRESHOLD) {
      setIsPulling(true);
      refetchArticles().finally(() => setIsPulling(false));
    }
  };

  const extractFailed = useExtractFailed();
  const extractAct = useExtractAction();

  const searchResults = useSearchArticles(searchQuery, { feed_id: filters.feed_id, is_saved: filters.is_saved });
  const markAllRead = useMarkAllRead();
  const updateArticle = useUpdateArticle();
  const { data: aiStatus } = useAiStatus();
  const aiAvailable = aiStatus?.available ?? false;
  const { data: tags } = useTags();
  const bulkDeleteTags = useBulkDeleteTags();

  const selectedTag = filters.tag_id != null ? tags?.find(t => t.id === filters.tag_id) : null;

  const isSearching = searchQuery.length > 0;
  const isLoading = isExtractFailedView
    ? extractFailed.isLoading
    : isSearching
      ? searchResults.isLoading
      : isArticlesLoading;

  const articles: Article[] = isExtractFailedView
    ? (extractFailed.data ?? [])
    : isSearching
      ? (searchResults.data?.items ?? [])
      : (data?.pages.flatMap(p => p.items) ?? []);

  const total = isExtractFailedView
    ? (extractFailed.data?.length ?? 0)
    : isSearching
      ? (searchResults.data?.total ?? 0)
      : (data?.pages[0]?.total ?? 0);

  // When a background refetch removes the selected article (e.g. Unread view
  // after mark-as-read), keep it visible by prepending the pinned copy so
  // prev/next navigation and the floating controls keep working.
  // In the extract-failed view we *want* the row to disappear immediately
  // after retry/skip/delete, so pinning is disabled there.
  const displayArticles = useMemo(() => {
    if (!selectedId) return articles;
    if (isExtractFailedView) return articles;
    const freshVersion = articles.find(a => a.id === selectedId);
    if (freshVersion) {
      pinnedArticleRef.current = freshVersion;
      return articles;
    }
    const pinned = pinnedArticleRef.current;
    if (pinned && pinned.id === selectedId) return [pinned, ...articles];
    return articles;
  }, [articles, selectedId, isExtractFailedView]);

  // Reset scroll on filter/search change, clear pinned article, and auto-select first article
  useEffect(() => {
    listRef.current?.scrollTo(0, 0);
    pinnedArticleRef.current = null;
    setSelectedId(null);
  }, [filters, searchQuery]);

  // Report current view total to the parent (used by the mobile header)
  useEffect(() => {
    onTotalChange?.(total);
  }, [total, onTotalChange]);

  // Auto-select the first article when the article list loads after a filter change.
  // モバイルでは Reader が画面を覆ってしまうため、ユーザーの明示的なタップまで
  // 自動選択はしない。デスクトップは並列表示なので従来通り先頭を開いて OK。
  useEffect(() => {
    if (selectedId != null) return;
    if (isLoading) return;
    if (typeof window !== 'undefined' && window.innerWidth < 768) return;
    if (displayArticles.length > 0) {
      setSelectedId(displayArticles[0].id);
    }
  }, [displayArticles, isLoading, selectedId]);

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    if (!sentinelRef.current || !hasNextPage || isSearching || isExtractFailedView) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !isFetchingNextPage) fetchNextPage();
      },
      { rootMargin: '200px' }
    );
    observer.observe(sentinelRef.current);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, isSearching, isExtractFailedView]);

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
        try { sessionStorage.setItem('snoreader_nav', JSON.stringify(filters)); } catch { /* ignore */ }
        window.location.reload();
        break;
      }
    }
  }, [displayArticles, selectedId, updateArticle, queryClient, filters]);

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
          {isExtractFailedView ? (
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className="text-xs font-semibold text-amber-600 dark:text-amber-400">⚠ 取得失敗</span>
              <span className="text-xs text-gray-400">{total}件</span>
              <div className="flex-1" />
              <button
                onClick={() => {
                  const targets = articles.filter(a => a.extract_status === 'error');
                  if (targets.length === 0) return;
                  if (!confirm(`error の ${targets.length} 件を再試行しますか？`)) return;
                  for (const a of targets) extractAct.mutate({ id: a.id, action: 'retry' });
                }}
                disabled={extractAct.isPending}
                className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50"
                title="error を全て再試行"
              >
                error 一括再試行
              </button>
              <button
                onClick={() => {
                  const targets = articles.filter(a => a.extract_status === 'forbidden');
                  if (targets.length === 0) return;
                  if (!confirm(`403 の ${targets.length} 件をスキップしますか？`)) return;
                  for (const a of targets) extractAct.mutate({ id: a.id, action: 'skip' });
                }}
                disabled={extractAct.isPending}
                className="text-xs text-gray-500 hover:text-gray-700 disabled:opacity-50"
                title="403 を全てスキップ (要約のみ)"
              >
                403 一括スキップ
              </button>
              <button
                onClick={() => {
                  const targets = articles.filter(a => a.extract_status === 'not_found');
                  if (targets.length === 0) return;
                  if (!confirm(`404 の ${targets.length} 件を削除しますか？`)) return;
                  for (const a of targets) extractAct.mutate({ id: a.id, action: 'delete' });
                }}
                disabled={extractAct.isPending}
                className="text-xs text-red-500 hover:text-red-700 disabled:opacity-50"
                title="404 を全て削除"
              >
                404 一括削除
              </button>
            </div>
          ) : (
          <>
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
          {/* Tag filter chips — only in Saved view. Toggles tag_id/untagged while preserving is_saved. */}
          {filters.is_saved && (
            <div className="flex flex-wrap gap-1">
              <button
                onClick={() => onFilterChange({
                  ...filters,
                  tag_id: undefined,
                  untagged: filters.untagged ? undefined : true,
                })}
                className={`px-1.5 py-0.5 rounded text-xs hover:bg-gray-200 dark:hover:bg-gray-800 ${
                  filters.untagged
                    ? 'bg-gray-200 dark:bg-gray-800 font-semibold text-gray-900 dark:text-gray-100'
                    : 'text-gray-500 dark:text-gray-400'
                }`}
              >
                {tagLang === 'ja' ? 'タグなし' : 'Untagged'}
              </button>
              {tags?.map((tag) => (
                <button
                  key={tag.id}
                  onClick={() => onFilterChange({
                    ...filters,
                    untagged: undefined,
                    tag_id: filters.tag_id === tag.id ? undefined : tag.id,
                  })}
                  className={`px-1.5 py-0.5 rounded text-xs hover:bg-gray-200 dark:hover:bg-gray-800 ${
                    filters.tag_id === tag.id
                      ? 'bg-gray-200 dark:bg-gray-800 font-semibold text-gray-900 dark:text-gray-100'
                      : 'text-gray-500 dark:text-gray-400'
                  }`}
                >
                  #{tagLang === 'ja' && tag.name_ja ? tag.name_ja : tag.name}
                </button>
              ))}
            </div>
          )}
          </>
          )}
        </div>

        {/* Pull-to-refresh indicator — pinned above the scrollable list */}
        <div
          className="overflow-hidden transition-all duration-150 flex items-center justify-center"
          style={{ height: isPulling ? '36px' : pullDistance > 0 ? `${Math.round(pullDistance * 0.5)}px` : '0px' }}
        >
          {isPulling
            ? <Spinner size="sm" />
            : pullDistance >= PULL_THRESHOLD
              ? <span className="text-xs text-blue-400">↑ 離して更新</span>
              : <span className="text-xs text-gray-400">↓ 引っ張って更新</span>
          }
        </div>

        {/* Article list */}
        <div
          ref={listRef}
          className="flex-1 overflow-y-auto"
          onTouchStart={handleListTouchStart}
          onTouchMove={handleListTouchMove}
          onTouchEnd={handleListTouchEnd}
        >
          {isLoading && <div className="flex justify-center p-6"><Spinner /></div>}
          {!isLoading && articles.length === 0 && (
            <p className="p-4 text-sm text-gray-400">No articles found</p>
          )}
          {displayArticles.map((article) => (
            <div key={article.id}>
              <ArticleCard
                article={article}
                isSelected={article.id === selectedId}
                onClick={() => setSelectedId(article.id)}
                dimRead={!filters.is_saved}
              />
              {isExtractFailedView && (
                <div className="flex flex-wrap items-center gap-1 px-3 pb-2 -mt-1 bg-white dark:bg-gray-950 border-b border-gray-100 dark:border-gray-800">
                  <ExtractStatusBadge status={article.extract_status} />
                  <div className="flex-1" />
                  <button
                    disabled={extractAct.isPending}
                    onClick={(e) => { e.stopPropagation(); extractAct.mutate({ id: article.id, action: 'retry' }); }}
                    className="text-xs px-2 py-0.5 rounded border border-blue-300 text-blue-600 hover:bg-blue-50 dark:border-blue-700 dark:text-blue-400 dark:hover:bg-blue-900/30 disabled:opacity-50"
                    title="再取得を試みる (一時的障害向け)"
                  >
                    再試行
                  </button>
                  <button
                    disabled={extractAct.isPending}
                    onClick={(e) => { e.stopPropagation(); extractAct.mutate({ id: article.id, action: 'skip' }); }}
                    className="text-xs px-2 py-0.5 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800 disabled:opacity-50"
                    title="本文抽出を諦めて RSS summary から要約する"
                  >
                    要約のみ
                  </button>
                  <button
                    disabled={extractAct.isPending}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!confirm(`記事を削除しますか？\n${article.title}`)) return;
                      extractAct.mutate({ id: article.id, action: 'delete' });
                    }}
                    className="text-xs px-2 py-0.5 rounded border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-900/30 disabled:opacity-50"
                    title="DB から削除"
                  >
                    削除
                  </button>
                </div>
              )}
            </div>
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
              onSelect={(id) => setSelectedId(id)}
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
