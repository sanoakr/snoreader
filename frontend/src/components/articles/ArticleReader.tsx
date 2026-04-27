import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getArticle } from '../../api/client';
import { useUpdateArticle, useSummarizeArticle, useSuggestTags, useAiStatus, useExtractContent } from '../../hooks/useArticles';
import { useAddTag, useRemoveTag, useTags } from '../../hooks/useTags';
import type { TagSuggestion } from '../../types';

interface Props {
  articleId: number;
  tagLang: 'en' | 'ja';
}

export function ArticleReader({ articleId, tagLang }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const summarizeTried = useRef(false);
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
  const { data: aiStatus } = useAiStatus();
  const { data: existingTags } = useTags();
  const [tagInput, setTagInput] = useState('');
  const [showTagInput, setShowTagInput] = useState(false);
  const [suggestedTags, setSuggestedTags] = useState<TagSuggestion[]>([]);

  const aiAvailable = aiStatus?.available ?? false;

  // 記事が変わったら summarize 試行フラグをリセット
  useEffect(() => {
    summarizeTried.current = false;
    setSuggestedTags([]);
  }, [articleId]);

  // 記事を開いたら自動で既読にする
  useEffect(() => {
    if (article && !article.is_read) {
      updateArticle.mutate({ id: article.id, data: { is_read: true } });
    }
  }, [article?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // スクロール位置をリセット
  useEffect(() => {
    containerRef.current?.scrollTo(0, 0);
  }, [articleId]);

  // AI 要約を自動生成（ai_summary がなければ）
  useEffect(() => {
    if (article && !article.ai_summary && aiAvailable && !summarizeTried.current) {
      summarizeTried.current = true;
      summarizeArticle.mutate(article.id);
    }
  }, [article?.id, aiAvailable]); // eslint-disable-line react-hooks/exhaustive-deps

  if (isLoading) {
    return <div className="p-6 text-gray-400">Loading...</div>;
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
    addTag.mutate({ articleId: article.id, name: tagInput.trim() });
    setTagInput('');
    setShowTagInput(false);
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
          if (willBeSaved && aiAvailable && !suggestedTags.length) {
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
    <div ref={containerRef} className="h-screen overflow-y-auto">
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

          {/* Tags — Saved 記事のみ表示 */}
          {article.is_saved && (
            <>
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
                      placeholder="tag name"
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
                {aiAvailable && !suggestedTags.length && (
                  <button
                    onClick={handleSuggestTags}
                    disabled={suggestTags.isPending}
                    className="text-xs text-purple-400 hover:text-purple-600 disabled:opacity-50"
                  >
                    {suggestTags.isPending ? 'AI...' : 'AI suggest'}
                  </button>
                )}
              </div>

              {/* AI suggested tags */}
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
            </>
          )}

          {/* AI Summary */}
          {article.ai_summary ? (
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-900/20 rounded text-sm text-gray-700 dark:text-gray-300">
              <span className="text-xs font-medium text-purple-500 block mb-1">AI Summary</span>
              {article.ai_summary}
            </div>
          ) : summarizeArticle.isPending ? (
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-900/20 rounded text-sm text-gray-400">
              <span className="text-xs font-medium text-purple-400 block mb-1">AI Summary</span>
              Summarizing...
            </div>
          ) : null}
        </header>

        {/* アイキャッチ画像 */}
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
            onClick={() => extractContent.mutate(article.id)}
            disabled={extractContent.isPending}
            className="text-xs text-gray-400 hover:text-blue-500 disabled:opacity-50"
            title="Re-fetch article content"
          >
            {extractContent.isPending ? '取得中...' : '↺ 本文再取得'}
          </button>
        </div>
        {article.content ? (
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
    </div>
  );
}
