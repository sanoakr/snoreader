import { useState } from 'react';
import {
  useSplitSuggestions,
  useApplySplitSuggestion,
  useDismissSplitSuggestion,
  useRefreshSplitSuggestions,
} from '../../hooks/useSplitSuggestions';
import type { SplitSuggestion } from '../../types';

const STRATEGY_LABEL: Record<string, string> = {
  demote_generic: 'タグを汎用ルールに降格(ジャンルを増やさない)',
  split_own_tags: '担当タグを兄弟ジャンルに分ける',
  promote_free_tags: '未ルールのタグを兄弟ジャンルに昇格',
};

// ジャンルの上限件数。バックエンドの検知閾値と同じ値を表示に使う
const GENRE_UNREAD_LIMIT = 50;

// 分割案の提示と適用。件数はバックエンドで実際に分類し直した実測値なので、
// ここでは計算せず表示するだけ。
export function SplitSuggestionPanel() {
  const { data: suggestions } = useSplitSuggestions();
  const apply = useApplySplitSuggestion();
  const dismiss = useDismissSplitSuggestion();
  const refresh = useRefreshSplitSuggestions();
  // {suggestionId: {childKey: label}} の編集中の値
  const [labels, setLabels] = useState<Record<number, Record<string, string>>>({});
  const [lastResult, setLastResult] = useState<string | null>(null);

  const items = suggestions ?? [];

  if (items.length === 0) return null;

  const editedLabels = (s: SplitSuggestion) => {
    const edited = labels[s.id] ?? {};
    const out: Record<string, string> = {};
    for (const child of s.children) out[child.key] = edited[child.key] ?? child.label_ja;
    return out;
  };

  return (
    <div className="space-y-2 px-1 mb-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-300">
          ジャンル分割の提案
        </span>
        <div className="flex-1" />
        <button
          type="button"
          className="text-xs text-blue-600 hover:underline disabled:opacity-50 dark:text-blue-400"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          再計算
        </button>
      </div>

      {items.map((s) => (
        <div
          key={s.id}
          className="rounded border border-amber-300 bg-amber-50 p-2 text-xs dark:border-amber-700 dark:bg-amber-950"
        >
          <div className="font-medium text-amber-900 dark:text-amber-200">
            ⚠ {s.genre_key} の未読が {s.before} 件(上限 {GENRE_UNREAD_LIMIT})
          </div>
          <div className="mt-1 text-gray-700 dark:text-gray-300">
            {STRATEGY_LABEL[s.strategy] ?? s.strategy}
            {' — '}
            {s.before} → {s.projected_max}
          </div>

          {/* demote_generic は子を持たず、降格するタグの一覧だけを示す */}
          {s.strategy === 'demote_generic' && s.demote_tags.length > 0 && (
            <div className="mt-1 text-gray-600 dark:text-gray-400">
              {s.demote_tags.map((tag) => `ルール ${tag} を汎用に降格`).join(', ')}
            </div>
          )}
          {s.strategy !== 'demote_generic' && s.demote_tags.length > 0 && (
            <div className="mt-1 text-gray-600 dark:text-gray-400">
              降格するタグ: {s.demote_tags.join(', ')}
            </div>
          )}

          {s.children.map((child) => (
            <div key={child.key} className="mt-1 flex items-center gap-1">
              <input
                className="w-28 rounded border border-gray-300 px-1 py-0.5 dark:border-gray-600 dark:bg-gray-800"
                value={(labels[s.id] ?? {})[child.key] ?? child.label_ja}
                onChange={(e) =>
                  setLabels((prev) => ({
                    ...prev,
                    [s.id]: { ...(prev[s.id] ?? {}), [child.key]: e.target.value },
                  }))
                }
              />
              <span className="text-gray-600 dark:text-gray-400">
                {child.key} ({child.tags.join(', ')}) — {child.estimated_unread} 件
              </span>
            </div>
          ))}

          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              className="rounded bg-blue-600 px-2 py-0.5 text-white hover:bg-blue-700 disabled:opacity-50"
              disabled={apply.isPending}
              onClick={() => {
                if (!confirm('この案を適用します。既存記事の再分類に十数秒かかります。')) return;
                apply.mutate(
                  { id: s.id, labels: editedLabels(s) },
                  {
                    onSuccess: (r) =>
                      setLastResult(
                        `ジャンル ${r.created} 件作成 / ルール ${r.moved} 件変更 / 記事 ${r.reclassified} 件再分類`,
                      ),
                  },
                );
              }}
            >
              適用
            </button>
            <button
              type="button"
              className="rounded border border-gray-300 px-2 py-0.5 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
              disabled={dismiss.isPending}
              onClick={() => dismiss.mutate(s.id)}
            >
              無視
            </button>
          </div>
        </div>
      ))}

      {lastResult && (
        <p className="text-xs text-gray-500 dark:text-gray-400">{lastResult}</p>
      )}
    </div>
  );
}
