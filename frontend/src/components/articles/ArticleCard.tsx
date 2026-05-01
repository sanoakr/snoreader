import type { Article } from '../../types';
import { useUpdateArticle } from '../../hooks/useArticles';

interface Props {
  article: Article;
  isSelected: boolean;
  onClick: () => void;
  dimRead?: boolean;
}

function timeAgo(dateStr: string | null): string {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

export function ArticleCard({ article, isSelected, onClick, dimRead = true }: Props) {
  const updateArticle = useUpdateArticle();

  const toggleSaved = (e: React.MouseEvent) => {
    e.stopPropagation();
    updateArticle.mutate({ id: article.id, data: { is_saved: !article.is_saved } });
  };

  return (
    <div
      data-article-id={article.id}
      onClick={onClick}
      className={`flex gap-3 p-3 cursor-pointer border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50 ${
        isSelected ? 'bg-blue-50 dark:bg-blue-900/20' : ''
      } ${dimRead && article.is_read ? 'opacity-60' : ''}`}
    >
      {/* Thumbnail */}
      {article.image_url && (
        <img
          src={article.image_url}
          alt=""
          className="w-16 h-16 object-cover rounded shrink-0 bg-gray-200 dark:bg-gray-700"
          loading="lazy"
          onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
        />
      )}

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-start justify-between gap-2">
          <h3 className={`text-sm leading-snug line-clamp-2 ${!dimRead || !article.is_read ? 'font-semibold text-gray-900 dark:text-gray-100' : ''}`}>
            {article.title}
          </h3>
          <button
            onClick={toggleSaved}
            className={`shrink-0 text-lg leading-none ${article.is_saved ? 'text-yellow-500' : 'text-gray-300 hover:text-yellow-400'}`}
            title={article.is_saved ? 'Unsave' : 'Save'}
          >
            {article.is_saved ? '★' : '☆'}
          </button>
        </div>

        <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">{article.summary}</p>

        <div className="flex items-center gap-2 mt-1 text-xs text-gray-400">
          <span className="truncate">{article.feed_title}</span>
          <span>{timeAgo(article.published_at)}</span>
          {article.rec_score != null && (
            <span className="text-amber-500 font-medium">★{article.rec_score.toFixed(1)}</span>
          )}
        </div>
      </div>
    </div>
  );
}
