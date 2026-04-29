import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getArticle } from '../../api/client';
import { useUpdateArticle, useSummarizeArticle, useSuggestTags, useExtractContent } from '../../hooks/useArticles';
import { Spinner } from '../common/Spinner';
import { useAddTag, useRemoveTag, useTags } from '../../hooks/useTags';
import { ArticleChatPanel } from './ArticleChatPanel';
import type { TagSuggestion } from '../../types';

interface Props {
  articleId: number;
  tagLang: 'en' | 'ja';
  aiAvailable: boolean;
  onPrev?: () => void;
  onNext?: () => void;
}

export function ArticleReader({ articleId, tagLang, aiAvailable, onPrev, onNext }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const touchStartX = useRef(0);
  const touchStartY = useRef(0);
  // Stable refs so the swipe effect only attaches once; always reads latest callbacks
  const onNextRef = useRef(onNext);
  const onPrevRef = useRef(onPrev);
  useEffect(() => { onNextRef.current = onNext; }, [onNext]);
  useEffect(() => { onPrevRef.current = onPrev; }, [onPrev]);
  const summarizeTried = useRef(false);
  const suggestTried = useRef(false);
  const { data: article, isLoading } = useQuery({
    queryKey: ['article', articleId],
    queryFn: () => getArticle(articleId),
  });
  const updateArticle = useUpdateArticle();
  const summarizeArticle = useSummarizeArticle();
  const extractContent = useExtractContent();
  const suggestTags = useSuggestTags();
  const addTag = useAddTag();
  const removeTag = useRemoveTag();
  const { data: existingTags } = useTags();
  const [tagInput, setTagInput] = useState('');
  const [showTagInput, setShowTagInput] = useState(false);
  const [suggestedTags, setSuggestedTags] = useState<TagSuggestion[]>([]);

  // Reset attempt flags when article changes
  useEffect(() => {
    summarizeTried.current = false;
    suggestTried.current = false;
    setSuggestedTags([]);
  }, [articleId]);

  // Auto-mark as read when article is opened
  useEffect(() => {
    if (article && !article.is_read) {
      updateArticle.mutate({ id: article.id, data: { is_read: true } });
    }
  }, [article?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Scroll to top on article change (iOS Safari needs explicit scrollTop reset)
  useEffect(() => {
    containerRef.current?.scrollTo(0, 0);
  }, [articleId]);

  // Swipe navigation (mobile) — runs once on mount; reads callbacks via stable refs
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onStart = (e: TouchEvent) => {
      touchStartX.current = e.touches[0].clientX;
      touchStartY.current = e.touches[0].clientY;
    };
    const onEnd = (e: TouchEvent) => {
      const dx = e.changedTouches[0].clientX - touchStartX.current;
      const dy = e.changedTouches[0].clientY - touchStartY.current;
      if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy)) return;
      if (dx < 0) onNextRef.current?.();
      else onPrevRef.current?.();
    };
    el.addEventListener('touchstart', onStart, { passive: true });
    el.addEventListener('touchend', onEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onStart);
      el.removeEventListener('touchend', onEnd);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-generate AI summary if not yet available
  useEffect(() => {
    if (article && !article.ai_summary && aiAvailable && !summarizeTried.current) {
      summarizeTried.current = true;
      summarizeArticle.mutate(article.id);
    }
  }, [article?.id, aiAvailable]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-suggest tags once AI summary is available (saved or not)
  useEffect(() => {
    if (article && article.ai_summary && aiAvailable && !suggestTried.current) {
      suggestTried.current = true;
      suggestTags.mutate(article.id, {
        onSuccess: (suggestions) => {
          const existing = existingTags ?? [];
          const autoAdd = suggestions.filter(s =>
            existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
          );
          const manual = suggestions.filter(s =>
            !existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
          );
          if (article.is_saved) {
            autoAdd.forEach(s => addTag.mutate({ articleId: article.id, name: s.name, name_ja: s.name_ja }));
            setSuggestedTags(manual);
          } else {
            setSuggestedTags([...autoAdd, ...manual]);
          }
        },
      });
    }
  }, [article?.id, article?.ai_summary, aiAvailable]); // eslint-disable-line react-hooks/exhaustive-deps

  if (isLoading) {
    return <div className="flex justify-center p-10"><Spinner /></div>;
  }

  if (!article) {
    return <div className="p-6 text-gray-400">Article not found</div>;
  }

  const publishedDate = article.published_at
    ? new Date(article.published_at).toLocaleDateString('ja-JP', {
        year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '';

  const handleAddTag = (e: React.FormEvent) => {
    e.preventDefault();
    if (!tagInput.trim()) return;
    addTag.mutate(
      { articleId: article.id, name: tagInput.trim() },
      {
        onSuccess: () => { setTagInput(''); setShowTagInput(false); },
        onError: (err) => { alert((err as Error).message); },
      }
    );
  };

  const handleSuggestTags = () => {
    suggestTags.mutate(article.id, {
      onSuccess: (suggestions) => {
        const existing = existingTags ?? [];
        const autoAdd = suggestions.filter(s =>
          existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
        );
        const manual = suggestions.filter(s =>
          !existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
        );
        autoAdd.forEach(s => addTag.mutate({ articleId: article.id, name: s.name, name_ja: s.name_ja }));
        setSuggestedTags(manual);
      },
    });
  };

  const handleAcceptTag = (suggestion: TagSuggestion) => {
    addTag.mutate({ articleId: article.id, name: suggestion.name, name_ja: suggestion.name_ja });
    setSuggestedTags(prev => prev.filter(t => t.name !== suggestion.name));
  };

  const handleSaveToggle = () => {
    const willBeSaved = !article.is_saved;
    updateArticle.mutate(
      { id: article.id, data: { is_saved: willBeSaved } },
      {
        onSuccess: () => {
          if (willBeSaved && aiAvailable && !suggestTried.current) {
            suggestTried.current = true;
            suggestTags.mutate(article.id, {
              onSuccess: (suggestions) => {
                const existing = existingTags ?? [];
                const autoAdd = suggestions.filter(s =>
                  existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
                );
                const manual = suggestions.filter(s =>
                  !existing.some(e => e.name.toLowerCase() === s.name.toLowerCase())
                );
                autoAdd.forEach(s => addTag.mutate({ articleId: article.id, name: s.name, name_ja: s.name_ja }));
                setSuggestedTags(manual);
              },
            });
          }
        },
      }
    );
  };

  return (
    <div ref={containerRef} className="h-screen overflow-y-auto pt-12 md:pt-0">
      <article className="max-w-3xl mx-auto p-6">
        <header className="mb-6">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 leading-tight mb-2">
            <a
              href={article.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-blue-600 dark:hover:text-blue-400"
            >
              {article.title}
            </a>
          </h1>
          <div className="flex items-center gap-3 text-sm text-gray-500">
            {article.feed_title && <span>{article.feed_title}</span>}
            {article.author && <span>by {article.author}</span>}
            {publishedDate && <span>{publishedDate}</span>}
          </div>
          <div className="mt-3">
            <button
              onClick={handleSaveToggle}
              disabled={updateArticle.isPending}
              className={`text-sm disabled:opacity-50 ${article.is_saved ? 'text-yellow-500' : 'text-gray-400 hover:text-yellow-500'}`}
            >
              {article.is_saved ? '★ Saved' : '☆ Save'}
            </button>
          </div>

          {/* Tags — only shown for saved articles */}
          {article.is_saved && (
            <div className="mt-3 flex items-center gap-1.5 flex-wrap">
              {article.tags?.map((tag) => (
                <span
                  key={tag.id}
                  className="inline-flex items-center gap-0.5 px-2 py-0.5 text-xs bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded"
                >
                  {tagLang === 'ja' && tag.name_ja ? tag.name_ja : tag.name}
                  <button
                    onClick={() => removeTag.mutate({ articleId: article.id, tagId: tag.id })}
                    className="text-gray-400 hover:text-red-500 ml-0.5"
                  >
                    ×
                  </button>
                </span>
              ))}
              {showTagInput ? (
                <form onSubmit={handleAddTag} className="inline-flex">
                  <input
                    type="text"
                    value={tagInput}
                    onChange={(e) => setTagInput(e.target.value)}
                    placeholder="英語 or 日本語"
                    className="w-24 px-1.5 py-0.5 text-xs border rounded dark:bg-gray-800 dark:border-gray-600"
                    autoFocus
                    onBlur={() => { if (!tagInput) setShowTagInput(false); }}
                    onKeyDown={(e) => { if (e.key === 'Escape') setShowTagInput(false); }}
                  />
                </form>
              ) : (
                <button
                  onClick={() => setShowTagInput(true)}
                  className="text-xs text-gray-400 hover:text-blue-500"
                >
                  + tag
                </button>
              )}
              {aiAvailable && !suggestedTags.length && !suggestTags.isPending && (
                <button
                  onClick={handleSuggestTags}
                  className="text-xs text-purple-400 hover:text-purple-600"
                >
                  AI suggest
                </button>
              )}
            </div>
          )}

          {/* AI suggested tags — shown for any article once available */}
          {suggestedTags.length > 0 && (
            <div className="mt-2 flex items-center gap-1.5 flex-wrap">
              <span className="text-xs text-purple-500">Suggested:</span>
              {suggestedTags.map((s) => (
                <button
                  key={s.name}
                  onClick={() => handleAcceptTag(s)}
                  className="px-2 py-0.5 text-xs border border-purple-300 dark:border-purple-700 text-purple-600 dark:text-purple-400 rounded hover:bg-purple-50 dark:hover:bg-purple-900/30"
                >
                  + {tagLang === 'ja' && s.name_ja ? s.name_ja : s.name}
                </button>
              ))}
              <button
                onClick={() => setSuggestedTags([])}
                className="text-xs text-gray-400 hover:text-gray-600"
              >
                dismiss
              </button>
            </div>
          )}

          {/* AI Summary */}
          {article.ai_summary ? (
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-900/20 rounded text-sm text-gray-700 dark:text-gray-300">
              <span className="text-xs font-medium text-purple-500 block mb-1">AI Summary</span>
              <ul className="space-y-1">
                {article.ai_summary.split('\n').filter(l => l.trim()).map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </div>
          ) : summarizeArticle.isPending ? (
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-900/20 rounded flex items-center gap-2">
              <Spinner size="sm" />
              <span className="text-xs text-purple-400">Summarizing...</span>
            </div>
          ) : null}
        </header>

        {/* Hero image */}
        {article.image_url && (
          <img
            src={article.image_url}
            alt=""
            referrerPolicy="no-referrer"
            className="w-full max-h-72 object-cover rounded-lg mb-4"
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
          />
        )}

        {/* Article content */}
        <div className="flex justify-end mb-2">
          <button
            onClick={() => extractContent.mutate(article.id, {
              onSuccess: () => { suggestTried.current = false; },
            })}
            disabled={extractContent.isPending}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-blue-500 disabled:opacity-50"
            title="Re-fetch article content"
          >
            {extractContent.isPending ? (
              <><Spinner size="sm" /><span>取得中...</span></>
            ) : '↺ 本文再取得'}
          </button>
        </div>
        {extractContent.isPending ? (
          <div className="flex justify-center py-12">
            <Spinner />
          </div>
        ) : article.content ? (
          <div
            className="prose dark:prose-invert max-w-none"
            dangerouslySetInnerHTML={{ __html: article.content }}
          />
        ) : (
          <div className="text-gray-600 dark:text-gray-400 leading-relaxed whitespace-pre-wrap">
            {article.summary || 'No content available.'}
          </div>
        )}
      </article>

      {aiAvailable && <ArticleChatPanel articleId={article.id} />}

      {/* Floating prev/next buttons (mobile only) */}
      {(onPrev || onNext) && (
        <div className="md:hidden fixed bottom-6 right-4 flex flex-col gap-2 z-30">
          <button
            onClick={onPrev}
            disabled={!onPrev}
            className="w-11 h-11 flex items-center justify-center rounded-full bg-gray-800/80 dark:bg-gray-200/80 text-white dark:text-gray-900 shadow-lg disabled:opacity-30 active:scale-95 transition-transform"
            aria-label="Previous article"
          >
            ↑
          </button>
          <button
            onClick={onNext}
            disabled={!onNext}
            className="w-11 h-11 flex items-center justify-center rounded-full bg-gray-800/80 dark:bg-gray-200/80 text-white dark:text-gray-900 shadow-lg disabled:opacity-30 active:scale-95 transition-transform"
            aria-label="Next article"
          >
            ↓
          </button>
        </div>
      )}
    </div>
  );
}
