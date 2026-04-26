import { useRef, useState } from 'react';
import { useFeeds, useCreateFeed, useDeleteFeed, useRefreshFeed, useImportOpml, useImportArticles } from '../../hooks/useFeeds';
import { opmlExportUrl } from '../../api/client';
import type { ArticleFilters } from '../../types';

interface Props {
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
  darkToggle?: React.ReactNode;
}

export function FeedSidebar({ filters, onFilterChange, darkToggle }: Props) {
  const { data: feeds, isLoading } = useFeeds();
  const createFeed = useCreateFeed();
  const deleteFeed = useDeleteFeed();
  const refreshFeed = useRefreshFeed();
  const importOpml = useImportOpml();
  const importArticles = useImportArticles();
  const [newUrl, setNewUrl] = useState('');
  const [showAdd, setShowAdd] = useState(false);
  const opmlFileRef = useRef<HTMLInputElement>(null);
  const articlesFileRef = useRef<HTMLInputElement>(null);

  const totalUnread = feeds?.reduce((s, f) => s + f.unread_count, 0) ?? 0;

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newUrl.trim()) return;
    await createFeed.mutateAsync(newUrl.trim());
    setNewUrl('');
    setShowAdd(false);
  };

  const handleOpmlImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) importOpml.mutate(file);
    e.target.value = '';
  };

  const handleArticlesImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) importArticles.mutate(file);
    e.target.value = '';
  };

  return (
    <aside className="w-64 shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 h-screen overflow-y-auto flex flex-col">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">SnoReader</h1>
        {darkToggle}
      </div>

      <nav className="flex-1 p-2 space-y-0.5">
        {/* All articles */}
        <button
          onClick={() => onFilterChange({ ...filters, feed_id: undefined, is_saved: undefined })}
          className={`w-full text-left px-3 py-2 rounded text-sm flex justify-between items-center hover:bg-gray-200 dark:hover:bg-gray-800 ${
            filters.feed_id == null && filters.is_saved == null ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
          }`}
        >
          <span>All</span>
          {totalUnread > 0 && (
            <span className="text-xs bg-blue-500 text-white rounded-full px-1.5 py-0.5 min-w-[20px] text-center">
              {totalUnread}
            </span>
          )}
        </button>

        {/* Saved */}
        <button
          onClick={() => onFilterChange({ ...filters, feed_id: undefined, is_saved: true })}
          className={`w-full text-left px-3 py-2 rounded text-sm hover:bg-gray-200 dark:hover:bg-gray-800 ${
            filters.is_saved === true ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
          }`}
        >
          Saved
        </button>

        <hr className="my-2 border-gray-200 dark:border-gray-700" />

        {/* Feed list */}
        {isLoading && <p className="text-xs text-gray-400 px-3">Loading...</p>}
        {feeds?.map((feed) => (
          <div key={feed.id} className="group flex items-center">
            <button
              onClick={() => onFilterChange({ ...filters, feed_id: feed.id, is_saved: undefined })}
              className={`flex-1 text-left px-3 py-1.5 rounded text-sm truncate flex items-center gap-2 hover:bg-gray-200 dark:hover:bg-gray-800 ${
                filters.feed_id === feed.id ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
              }`}
            >
              {feed.favicon_url ? (
                <img src={feed.favicon_url} alt="" className="w-4 h-4 shrink-0 rounded" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
              ) : (
                <span className="w-4 h-4 shrink-0 rounded bg-gray-300 dark:bg-gray-600 text-[10px] flex items-center justify-center text-gray-500 dark:text-gray-400">
                  {(feed.title || feed.url)[0]?.toUpperCase()}
                </span>
              )}
              <span className="truncate flex-1">{feed.title || feed.url}</span>
              {feed.unread_count > 0 && (
                <span className="text-xs text-gray-500 ml-1">{feed.unread_count}</span>
              )}
            </button>
            <div className="hidden group-hover:flex items-center gap-0.5 pr-1">
              <button
                onClick={() => refreshFeed.mutate(feed.id)}
                className="text-gray-400 hover:text-blue-500 p-0.5"
                title="Refresh"
              >
                ↻
              </button>
              <button
                onClick={() => { if (confirm(`Delete "${feed.title || feed.url}"?`)) deleteFeed.mutate(feed.id); }}
                className="text-gray-400 hover:text-red-500 p-0.5"
                title="Delete"
              >
                ×
              </button>
            </div>
          </div>
        ))}
      </nav>

      {/* Add feed + Import/Export */}
      <div className="p-2 border-t border-gray-200 dark:border-gray-700 space-y-1">
        {showAdd ? (
          <form onSubmit={handleAdd} className="space-y-2">
            <input
              type="url"
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              placeholder="https://example.com/feed.xml"
              className="w-full px-2 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
              autoFocus
            />
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={createFeed.isPending}
                className="flex-1 px-2 py-1 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
              >
                {createFeed.isPending ? 'Adding...' : 'Add'}
              </button>
              <button
                type="button"
                onClick={() => setShowAdd(false)}
                className="px-2 py-1 text-sm border rounded hover:bg-gray-100 dark:hover:bg-gray-800"
              >
                Cancel
              </button>
            </div>
            {createFeed.isError && (
              <p className="text-xs text-red-500">{(createFeed.error as Error).message}</p>
            )}
          </form>
        ) : (
          <>
            <button
              onClick={() => setShowAdd(true)}
              className="w-full px-3 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-800 rounded"
            >
              + Add Feed
            </button>
            <div className="flex gap-1">
              <button
                onClick={() => opmlFileRef.current?.click()}
                disabled={importOpml.isPending}
                className="flex-1 px-2 py-1.5 text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800 rounded disabled:opacity-50"
              >
                {importOpml.isPending ? 'Importing...' : 'Import OPML'}
              </button>
              <a
                href={opmlExportUrl}
                download
                className="flex-1 px-2 py-1.5 text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800 rounded text-center"
              >
                Export OPML
              </a>
            </div>
            <button
              onClick={() => articlesFileRef.current?.click()}
              disabled={importArticles.isPending}
              className="w-full px-2 py-1.5 text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800 rounded disabled:opacity-50"
            >
              {importArticles.isPending ? 'Importing...' : 'Import Saved Articles (JSON)'}
            </button>
            {importOpml.isSuccess && (
              <p className="text-xs text-green-600">
                Imported {importOpml.data.created} feeds ({importOpml.data.skipped} skipped)
              </p>
            )}
            {importArticles.isSuccess && (
              <p className="text-xs text-green-600">
                Imported {importArticles.data.articles_created} articles, {importArticles.data.feeds_created} feeds
              </p>
            )}
            {importArticles.isError && (
              <p className="text-xs text-red-500">{(importArticles.error as Error).message}</p>
            )}
          </>
        )}
        <input ref={opmlFileRef} type="file" accept=".opml,.xml" onChange={handleOpmlImport} className="hidden" />
        <input ref={articlesFileRef} type="file" accept=".json" onChange={handleArticlesImport} className="hidden" />
      </div>
    </aside>
  );
}
