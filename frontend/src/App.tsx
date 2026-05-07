import { useEffect, useMemo, useState } from 'react';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { FeedSidebar } from './components/layout/FeedSidebar';
import { ArticleList } from './components/articles/ArticleList';
import { useFeeds } from './hooks/useFeeds';
import { useTags } from './hooks/useTags';
import type { ArticleFilters } from './types';

const SESSION_NAV_KEY = 'snoreader_nav';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

function useDarkMode() {
  const [dark, setDark] = useState(() => {
    if (typeof window === 'undefined') return false;
    const stored = localStorage.getItem('theme');
    if (stored) return stored === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    localStorage.setItem('theme', dark ? 'dark' : 'light');
  }, [dark]);

  return [dark, () => setDark(d => !d)] as const;
}

function useTagLang() {
  const [lang, setLang] = useState<'en' | 'ja'>(() => {
    return (localStorage.getItem('tagLang') as 'en' | 'ja') ?? 'en';
  });

  const toggle = () => setLang(l => {
    const next = l === 'en' ? 'ja' : 'en';
    localStorage.setItem('tagLang', next);
    return next;
  });

  return [lang, toggle] as const;
}

function filtersKey(f: ArticleFilters): string {
  return JSON.stringify(Object.fromEntries(
    Object.entries(f).filter(([, v]) => v !== undefined).sort()
  ));
}

function AppInner() {
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<ArticleFilters>(() => {
    try {
      const stored = sessionStorage.getItem(SESSION_NAV_KEY);
      if (stored) {
        sessionStorage.removeItem(SESSION_NAV_KEY);
        return JSON.parse(stored) as ArticleFilters;
      }
    } catch { /* ignore */ }
    return { is_read: false };
  });
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dark, toggleDark] = useDarkMode();
  const [tagLang, toggleTagLang] = useTagLang();
  const [viewTotal, setViewTotal] = useState(0);
  const { data: feeds } = useFeeds();
  const { data: tags } = useTags();

  const totalUnread = (feeds ?? []).reduce((s, f) => s + f.unread_count, 0);

  const viewLabel = useMemo(() => {
    if (filters.recommended) return 'Recommend';
    if (filters.unrecommended) return 'Unrecommend';
    if (filters.is_saved) {
      if (filters.untagged) return tagLang === 'ja' ? 'Saved / タグなし' : 'Saved / Untagged';
      if (filters.tag_id != null) {
        const t = tags?.find(x => x.id === filters.tag_id);
        const name = t ? (tagLang === 'ja' && t.name_ja ? t.name_ja : t.name) : '';
        return `Saved / #${name}`;
      }
      return 'Saved';
    }
    if (filters.feed_id != null) {
      const f = feeds?.find(x => x.id === filters.feed_id);
      return f?.title || f?.url || 'Feed';
    }
    if (filters.is_read === false) return 'Unread';
    if (filters.is_read === true) return 'Read';
    return 'All';
  }, [filters, feeds, tags, tagLang]);

  const handleFilterChange = (f: ArticleFilters) => {
    if (filtersKey(f) === filtersKey(filters)) {
      queryClient.invalidateQueries({ queryKey: ['articles'] });
    } else {
      setFilters(f);
    }
    setSidebarOpen(false);
  };

  return (
    <div className="flex h-screen bg-white dark:bg-gray-950 text-gray-700 dark:text-gray-300">
      {/* Mobile header */}
      <div className="fixed top-0 left-0 right-0 z-30 flex items-center gap-2 p-2 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 md:hidden">
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <div className="flex-1 min-w-0 flex items-baseline gap-2">
          <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{viewLabel}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400 shrink-0 tabular-nums">
            {viewTotal}件 / 未読 {totalUnread}
          </span>
        </div>
        <button onClick={toggleDark} className="p-1.5 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-sm">
          {dark ? '☀' : '☾'}
        </button>
      </div>

      {/* Sidebar overlay (mobile) */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 bg-black/30 md:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Sidebar */}
      <div className={`fixed z-50 md:relative md:z-auto transition-transform duration-200 ${
        sidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'
      }`}>
        <FeedSidebar
          filters={filters}
          onFilterChange={handleFilterChange}
          tagLang={tagLang}
          onToggleTagLang={toggleTagLang}
          darkToggle={
            <button onClick={toggleDark} className="hidden md:block p-1 rounded hover:bg-gray-200 dark:hover:bg-gray-800 text-sm" title={dark ? 'Light mode' : 'Dark mode'}>
              {dark ? '☀' : '☾'}
            </button>
          }
        />
      </div>

      {/* Main content */}
      <div className="flex-1 min-w-0 pt-12 md:pt-0">
        <ArticleList filters={filters} onFilterChange={setFilters} tagLang={tagLang} onTotalChange={setViewTotal} />
      </div>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppInner />
    </QueryClientProvider>
  );
}
