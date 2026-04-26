import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getArticle } from '../../api/client';
import { useUpdateArticle, useExtractArticle, useSummarizeArticle, useSuggestTags, useAiStatus } from '../../hooks/useArticles';
import { useAddTag, useRemoveTag } from '../../hooks/useTags';

interface Props {
  articleId: number;
}

export function ArticleReader({ articleId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { data: article, isLoading } = useQuery({
    queryKey: ['article', articleId],
    queryFn: () => getArticle(articleId),
  });
  const updateArticle = useUpdateArticle();
  const extractArticle = useExtractArticle();
  const summarizeArticle = useSummarizeArticle();
  const suggestTags = useSuggestTags();
  const addTag = useAddTag();
  const removeTag = useRemoveTag();
  const { data: aiStatus } = useAiStatus();
  const [tagInput, setTagInput] = useState('');
  const [showTagInput, setShowTagInput] = useState(false);
  const [suggestedTags, setSuggestedTags] = useState<string[]>([]);

  useEffect(() => {
    if (article && !article.is_read) {
      updateArticle.mutate({ id: article.id, data: { is_read: true } });
    }
  }, [article?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    containerRef.current?.scrollTo(0, 0);
    setSuggestedTags([]);
  }, [articleId]);

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
      onSuccess: (tags) => setSuggestedTags(tags),
    });
  };

  const handleAcceptTag = (name: string) => {
    addTag.mutate({ articleId: article.id, name });
    setSuggestedTags(prev => prev.filter(t => t !== name));
  };

  const aiAvailable = aiStatus?.available ?? false;

  return (
    <div ref={containerRef} className="h-screen overflow-y-auto">
      <article className="max-w-3xl mx-auto p-6">
        <header className="mb-6">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 leading-tight mb-2">
            {article.title}
          </h1>
          <div className="flex items-center gap-3 text-sm text-gray-500">
            {article.feed_title && <span>{article.feed_title}</span>}
            {article.author && <span>by {article.author}</span>}
            {publishedDate && <span>{publishedDate}</span>}
          </div>
          <div className="mt-3 flex gap-3">
            <a
              href={article.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-blue-500 hover:text-blue-700"
            >
              Open original →
            </a>
            <button
              onClick={() => updateArticle.mutate({ id: article.id, data: { is_saved: !article.is_saved } })}
              className={`text-sm ${article.is_saved ? 'text-yellow-500' : 'text-gray-400 hover:text-yellow-500'}`}
            >
              {article.is_saved ? '★ Saved' : '☆ Save'}
            </button>
          </div>

          {/* Tags */}
          <div className="mt-3 flex items-center gap-1.5 flex-wrap">
            {article.tags?.map((tag) => (
              <span
                key={tag.id}
                className="inline-flex items-center gap-0.5 px-2 py-0.5 text-xs bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 rounded"
              >
                {tag.name}
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
              {suggestedTags.map((tag) => (
                <button
                  key={tag}
                  onClick={() => handleAcceptTag(tag)}
                  className="px-2 py-0.5 text-xs border border-purple-300 dark:border-purple-700 text-purple-600 dark:text-purple-400 rounded hover:bg-purple-50 dark:hover:bg-purple-900/30"
                >
                  + {tag}
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
          {article.ai_summary && (
            <div className="mt-4 p-3 bg-purple-50 dark:bg-purple-900/20 rounded text-sm text-gray-700 dark:text-gray-300">
              <span className="text-xs font-medium text-purple-500 block mb-1">AI Summary</span>
              {article.ai_summary}
            </div>
          )}
          {aiAvailable && !article.ai_summary && (
            <button
              onClick={() => summarizeArticle.mutate(article.id)}
              disabled={summarizeArticle.isPending}
              className="mt-3 text-xs text-purple-400 hover:text-purple-600 disabled:opacity-50"
            >
              {summarizeArticle.isPending ? 'Summarizing...' : 'AI summarize'}
            </button>
          )}
        </header>

        {/* Article content */}
        {article.content ? (
          <div
            className="prose dark:prose-invert max-w-none"
            dangerouslySetInnerHTML={{ __html: article.content }}
          />
        ) : (
          <div>
            <button
              onClick={() => extractArticle.mutate(article.id)}
              disabled={extractArticle.isPending}
              className="mb-4 px-3 py-1.5 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
            >
              {extractArticle.isPending ? 'Extracting...' : 'Extract full content'}
            </button>
            <div className="text-gray-600 dark:text-gray-400 leading-relaxed whitespace-pre-wrap">
              {article.summary || 'No content available.'}
            </div>
          </div>
        )}
      </article>
    </div>
  );
}
