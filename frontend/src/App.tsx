import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FeedSidebar } from './components/layout/FeedSidebar';
import { ArticleList } from './components/articles/ArticleList';
import type { ArticleFilters } from './types';

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

function AppInner() {
  const [filters, setFilters] = useState<ArticleFilters>({});
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dark, toggleDark] = useDarkMode();
  const [tagLang, toggleTagLang] = useTagLang();

  const handleFilterChange = (f: ArticleFilters) => {
    setFilters(f);
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
        <span className="text-sm font-bold flex-1">SnoReader</span>
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
        <ArticleList filters={filters} onFilterChange={setFilters} tagLang={tagLang} />
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
