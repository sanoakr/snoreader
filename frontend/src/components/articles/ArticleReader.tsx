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

  // Swipe navigation (mobile) — declarative handlers so they attach once the
  // container div actually renders (useEffect with [] never re-ran after the
  // loading spinner was replaced by the real article DOM).
  const handleTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    touchStartX.current = e.touches[0].clientX;
    touchStartY.current = e.touches[0].clientY;
  };
  const handleTouchEnd = (e: React.TouchEvent<HTMLDivElement>) => {
    const dx = e.changedTouches[0].clientX - touchStartX.current;
    const dy = e.changedTouches[0].clientY - touchStartY.current;
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy)) return;
    if (dx < 0) onNextRef.current?.();
    else onPrevRef.current?.();
  };

  // Auto-generate AI summary if not yet available
  useEffect(() => {
    if (article && !article.ai_summary && aiAvailable && !summarizeTried.current) {
      summarizeTried.current = true;
      summarizeArticle.mutate(article.id);
    }
  }, [article?.id, aiAvailable]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-suggest tags once AI summary is available (saved or not).
  // Existing tags come first and are flagged so the UI can color-code them.
  useEffect(() => {
    if (article && article.ai_summary && aiAvailable && !suggestTried.current) {
      suggestTried.current = true;
      suggestTags.mutate(article.id, {
        onSuccess: (suggestions) => {
          const existing = existingTags ?? [];
          const merged = suggestions.map(s => ({
            ...s,
            existing: existing.some(e => e.name.toLowerCase() === s.name.toLowerCase()),
          }));
          merged.sort((a, b) => Number(!!b.existing) - Number(!!a.existing));
          setSuggestedTags(merged);
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
        const merged = suggestions.map(s => ({
          ...s,
          existing: existing.some(e => e.name.toLowerCase() === s.name.toLowerCase()),
        }));
        merged.sort((a, b) => Number(!!b.existing) - Number(!!a.existing));
        setSuggestedTags(merged);
      },
    });
  };

  const handleAcceptTag = (suggestion: TagSuggestion) => {
    addTag.mutate({ articleId: article.id, name: suggestion.name, name_ja: suggestion.name_ja });
    setSuggestedTags(prev => prev.filter(t => t.name !== suggestion.name));
  };

  // Existing-tag autocomplete for the manual input field.
  const attachedTagIds = new Set(article.tags?.map(t => t.id) ?? []);
  const _q = tagInput.trim().toLowerCase();
  const tagCandidates = _q
    ? (existingTags ?? [])
        .filter(t =>
          !attachedTagIds.has(t.id) &&
          (t.name.toLowerCase().includes(_q) || (t.name_ja ?? '').toLowerCase().includes(_q))
        )
        .slice(0, 8)
    : [];

  const handlePickExistingTag = (t: { name: string; name_ja: string | null }) => {
    addTag.mutate(
      { articleId: article.id, name: t.name, name_ja: t.name_ja },
      {
        onSuccess: () => { setTagInput(''); setShowTagInput(false); },
        onError: (err) => { alert((err as Error).message); },
      }
    );
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
                const merged = suggestions.map(s => ({
                  ...s,
                  existing: existing.some(e => e.name.toLowerCase() === s.name.toLowerCase()),
                }));
                merged.sort((a, b) => Number(!!b.existing) - Number(!!a.existing));
                setSuggestedTags(merged);
              },
            });
          }
        },
      }
    );
  };

  return (
    <div
      ref={containerRef}
      onTouchStart={handleTouchStart}
      onTouchEnd={handleTouchEnd}
      className="h-screen overflow-y-auto pt-12 md:pt-0"
    >
      <article className="relative max-w-3xl mx-auto p-6">
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
          <div className="mt-3 flex items-center gap-4">
            <button
              onClick={handleSaveToggle}
              disabled={updateArticle.isPending}
              className={`text-sm disabled:opacity-50 ${article.is_saved ? 'text-yellow-500' : 'text-gray-400 hover:text-yellow-500'}`}
            >
              {article.is_saved ? '★ Saved' : '☆ Save'}
            </button>
            <button
              onClick={() => updateArticle.mutate({ id: article.id, data: { is_read: !article.is_read } })}
              disabled={updateArticle.isPending}
              title={article.is_read ? 'Mark as unread' : 'Mark as read'}
              className={`text-sm disabled:opacity-50 ${article.is_read ? 'text-gray-400 hover:text-blue-500' : 'text-blue-500 hover:text-gray-400'}`}
            >
              {article.is_read ? '○ Unread' : '● Read'}
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
                <div className="inline-flex flex-col gap-1">
                  <form onSubmit={handleAddTag}>
                    <input
                      type="text"
                      value={tagInput}
                      onChange={(e) => setTagInput(e.target.value)}
                      placeholder="英語 or 日本語"
                      className="w-32 px-1.5 py-0.5 text-xs border rounded dark:bg-gray-800 dark:border-gray-600"
                      autoFocus
                      onBlur={() => { setTimeout(() => { if (!tagInput) setShowTagInput(false); }, 150); }}
                      onKeyDown={(e) => { if (e.key === 'Escape') setShowTagInput(false); }}
                    />
                  </form>
                  {tagInput.trim() && tagCandidates.length > 0 && (
                    <div className="flex flex-wrap gap-1 max-w-xs">
                      {tagCandidates.map((t) => (
                        <button
                          key={t.id}
                          type="button"
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => handlePickExistingTag(t)}
                          className="px-1.5 py-0.5 text-xs border border-blue-300 dark:border-blue-700 text-blue-600 dark:text-blue-400 rounded hover:bg-blue-50 dark:hover:bg-blue-900/30"
                        >
                          {tagLang === 'ja' && t.name_ja ? t.name_ja : t.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
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

          {/* AI suggested tags — shown for any article once available.
              Existing tags (blue) are listed before newly generated ones (purple). */}
          {suggestedTags.length > 0 && (
            <div className="mt-2 flex items-center gap-1.5 flex-wrap">
              <span className="text-xs text-gray-500 dark:text-gray-400">Suggested:</span>
              {suggestedTags.map((s) => (
                <button
                  key={s.name}
                  onClick={() => handleAcceptTag(s)}
                  title={s.existing ? 'Existing tag' : 'New tag (AI generated)'}
                  className={
                    s.existing
                      ? 'px-2 py-0.5 text-xs border border-blue-300 dark:border-blue-700 text-blue-600 dark:text-blue-400 rounded hover:bg-blue-50 dark:hover:bg-blue-900/30'
                      : 'px-2 py-0.5 text-xs border border-purple-300 dark:border-purple-700 text-purple-600 dark:text-purple-400 rounded hover:bg-purple-50 dark:hover:bg-purple-900/30'
                  }
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

      {/* Floating prev/next buttons — mobile only, pinned above the chat panel.
          Keep the frame visible even when one direction is disabled so the
          user always has an affordance to navigate. */}
      <div
        className="md:hidden fixed right-3 flex flex-col gap-2 z-20"
        style={{ bottom: 'calc(env(safe-area-inset-bottom, 0px) + 4rem)' }}
      >
          <button
            onClick={onPrev}
            disabled={!onPrev}
            className="w-11 h-11 flex items-center justify-center rounded-full bg-gray-800/80 dark:bg-gray-200/80 text-white dark:text-gray-900 shadow-lg disabled:opacity-30 active:scale-95 transition-transform backdrop-blur"
            aria-label="Previous article"
          >
            ↑
          </button>
          <button
            onClick={onNext}
            disabled={!onNext}
            className="w-11 h-11 flex items-center justify-center rounded-full bg-gray-800/80 dark:bg-gray-200/80 text-white dark:text-gray-900 shadow-lg disabled:opacity-30 active:scale-95 transition-transform backdrop-blur"
            aria-label="Next article"
          >
            ↓
          </button>
      </div>

      {aiAvailable && <ArticleChatPanel articleId={article.id} />}
    </div>
  );
}
