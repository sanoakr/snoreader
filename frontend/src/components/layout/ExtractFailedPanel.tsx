import { useState } from 'react';
import { Spinner } from '../common/Spinner';
import { useExtractAction, useExtractFailed } from '../../hooks/useArticles';
import type { Article, ExtractAction } from '../../types';

interface Props {
  onClose: () => void;
}

const STATUS_LABEL: Record<string, string> = {
  not_found: '404 (削除済み)',
  forbidden: '403 (アクセス拒否)',
  error: 'エラー',
  empty: '本文空 (JS/PDF 等)',
  skipped: 'スキップ済み',
};

const STATUS_COLOR: Record<string, string> = {
  not_found: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  forbidden: 'bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300',
  error: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  empty: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300',
  skipped: 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300',
};

function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  const label = STATUS_LABEL[status] ?? status;
  const color = STATUS_COLOR[status] ?? STATUS_COLOR.error;
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${color}`}>{label}</span>
  );
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch {
    return '';
  }
}

function domainOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

export function ExtractFailedPanel({ onClose }: Props) {
  const { data, isLoading } = useExtractFailed();
  const act = useExtractAction();
  const [busyId, setBusyId] = useState<number | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const runAction = (article: Article, action: ExtractAction) => {
    if (action === 'delete' && !confirm(`記事を削除しますか？\n${article.title}`)) return;
    setBusyId(article.id);
    act.mutate(
      { id: article.id, action },
      { onSettled: () => setBusyId(null) },
    );
  };

  const bulkAction = (status: string, action: ExtractAction) => {
    const targets = (data ?? []).filter(a => a.extract_status === status);
    if (targets.length === 0) return;
    const label = STATUS_LABEL[status] ?? status;
    const verb = action === 'delete' ? '削除' : action === 'skip' ? 'スキップ' : '再試行';
    if (!confirm(`${label} の ${targets.length} 件を${verb}しますか？`)) return;
    for (const a of targets) {
      act.mutate({ id: a.id, action });
    }
  };

  const grouped: Record<string, Article[]> = {};
  for (const a of data ?? []) {
    const key = a.extract_status ?? 'error';
    (grouped[key] ??= []).push(a);
  }
  const statusOrder = ['not_found', 'forbidden', 'error', 'empty', 'skipped'];

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-3xl max-h-[85vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            本文取得に失敗した記事
            {data && <span className="ml-2 text-sm text-gray-500">({data.length})</span>}
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xl leading-none px-2"
            title="Close"
          >
            ×
          </button>
        </div>

        <div className="p-4 overflow-y-auto flex-1">
          {isLoading && (
            <div className="flex justify-center py-8"><Spinner /></div>
          )}
          {!isLoading && (data?.length ?? 0) === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-8">
              取得失敗の記事はありません。
            </p>
          )}

          {statusOrder.map(status => {
            const items = grouped[status];
            if (!items || items.length === 0) return null;
            return (
              <section key={status} className="mb-6 last:mb-0">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 flex items-center gap-2">
                    <StatusBadge status={status} />
                    <span className="text-xs text-gray-500">{items.length} 件</span>
                  </h3>
                  <div className="flex gap-2">
                    {status === 'error' && (
                      <button
                        onClick={() => bulkAction(status, 'retry')}
                        className="text-xs text-blue-500 hover:text-blue-700"
                      >
                        一括再試行
                      </button>
                    )}
                    {status === 'forbidden' && (
                      <button
                        onClick={() => bulkAction(status, 'skip')}
                        className="text-xs text-gray-500 hover:text-gray-700"
                      >
                        一括スキップ
                      </button>
                    )}
                    {status === 'not_found' && (
                      <button
                        onClick={() => bulkAction(status, 'delete')}
                        className="text-xs text-red-500 hover:text-red-700"
                      >
                        一括削除
                      </button>
                    )}
                  </div>
                </div>
                <ul className="space-y-1.5">
                  {items.map(article => {
                    const expanded = expandedId === article.id;
                    const pubDate = formatDate(article.published_at);
                    const domain = domainOf(article.url);
                    return (
                      <li
                        key={article.id}
                        className="rounded border border-gray-200 dark:border-gray-700"
                      >
                        <div className="flex items-start gap-2 p-2">
                          <button
                            onClick={() => setExpandedId(expanded ? null : article.id)}
                            className="shrink-0 w-5 h-5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 text-xs"
                            title={expanded ? '閉じる' : '詳細を表示'}
                            aria-label={expanded ? 'Collapse' : 'Expand'}
                          >
                            {expanded ? '▼' : '▶'}
                          </button>
                          <div className="flex-1 min-w-0">
                            <a
                              href={article.url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-sm text-gray-900 dark:text-gray-100 hover:underline line-clamp-2"
                              title={article.title}
                            >
                              {article.title || '(untitled)'}
                            </a>
                            <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                              <span className="truncate">{article.feed_title ?? '—'}</span>
                              <span>·</span>
                              <span className="truncate">{domain}</span>
                              {pubDate && <><span>·</span><span>{pubDate}</span></>}
                            </div>
                          </div>
                          <div className="flex gap-1 shrink-0">
                            <button
                              disabled={busyId === article.id}
                              onClick={() => runAction(article, 'retry')}
                              className="text-xs px-2 py-1 rounded border border-blue-300 text-blue-600 hover:bg-blue-50 dark:border-blue-700 dark:text-blue-400 dark:hover:bg-blue-900/30 disabled:opacity-50"
                              title="再取得を試みる (一時的障害向け)"
                            >
                              再試行
                            </button>
                            <button
                              disabled={busyId === article.id}
                              onClick={() => runAction(article, 'skip')}
                              className="text-xs px-2 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800 disabled:opacity-50"
                              title="本文抽出を諦めて RSS summary から要約する"
                            >
                              要約のみ
                            </button>
                            <button
                              disabled={busyId === article.id}
                              onClick={() => runAction(article, 'delete')}
                              className="text-xs px-2 py-1 rounded border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-900/30 disabled:opacity-50"
                              title="DB から削除"
                            >
                              削除
                            </button>
                          </div>
                        </div>
                        {expanded && (
                          <div className="px-2 pb-2 pl-9 space-y-2 border-t border-gray-100 dark:border-gray-800 pt-2">
                            <div className="text-xs text-gray-500 dark:text-gray-400 break-all">
                              <span className="text-gray-400">URL:</span>{' '}
                              <a
                                href={article.url}
                                target="_blank"
                                rel="noreferrer"
                                className="text-blue-500 hover:underline"
                              >
                                {article.url}
                              </a>
                            </div>
                            {article.author && (
                              <div className="text-xs text-gray-500 dark:text-gray-400">
                                <span className="text-gray-400">Author:</span> {article.author}
                              </div>
                            )}
                            {article.summary && (
                              <div className="text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
                                <span className="text-gray-400">Summary:</span><br />
                                {article.summary}
                              </div>
                            )}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}
