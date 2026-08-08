import { useState } from 'react';
import {
  useGenres,
  useCreateGenre,
  useUpdateGenre,
  useDeleteGenre,
  useCreateGenreRule,
  useDeleteGenreRule,
} from '../../hooks/useGenres';
import type { ArticleFilters } from '../../types';

// 新規ジャンルの初期優先度。数字が小さいほど分類時に優先される。
const DEFAULT_NEW_GENRE_PRIORITY = 100;

interface Props {
  // 「分類できなかった記事（その他）を見る」導線用。呼び出し元でモーダルを閉じて filter を切り替える。
  onNavigateToOther: () => void;
  // 削除したジャンルが現在表示中のビューと同じ場合に、その絞り込みから抜けるために必要
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
}

// サイドバーの「ジャンル管理」セクションの内容。辞書（タグ→ジャンルの割り当て、優先順位）を編集する。
// FeedSidebar.tsx が肥大化していたため別ファイルに切り出した。
export function GenreManagerModal({ onNavigateToOther, filters, onFilterChange }: Props) {
  const { data: genres } = useGenres();
  const createGenre = useCreateGenre();
  const updateGenre = useUpdateGenre();
  const deleteGenre = useDeleteGenre();
  const createRule = useCreateGenreRule();
  const deleteRule = useDeleteGenreRule();

  const [newTag, setNewTag] = useState('');
  const [newRuleGenreId, setNewRuleGenreId] = useState<number | null>(null);
  const [newIsGeneric, setNewIsGeneric] = useState(false);
  const [lastReclassified, setLastReclassified] = useState<number | null>(null);
  const [newGenreKey, setNewGenreKey] = useState('');
  const [newGenreLabel, setNewGenreLabel] = useState('');

  // タグ追加フォームの <select> は空の選択肢を持たないため、ユーザーがまだ選択していない間は
  // 先頭のジャンルを既定値として使う（state 自体は未選択のまま保つ。setState-in-effect を避けるため）。
  const effectiveRuleGenreId = newRuleGenreId ?? genres?.[0]?.id ?? null;

  return (
    <div className="space-y-1 px-1">
      {genres?.map((g) => (
        <div key={g.id} className="border-b border-gray-200 dark:border-gray-700 py-2">
          <div className="flex items-center gap-2">
            <input
              defaultValue={g.label_ja}
              onBlur={(e) => {
                // 再分類（最大 10 秒）が進行中は古い g.label_ja からの差分判定で
                // 二重にミューテーションを飛ばさないようガードする
                if (updateGenre.isPending) return;
                const v = e.target.value.trim();
                if (v && v !== g.label_ja) {
                  updateGenre.mutate(
                    { id: g.id, label_ja: v },
                    { onSuccess: (res) => setLastReclassified(res.reclassified) },
                  );
                }
              }}
              className="text-sm px-1 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
            />
            <span className="text-xs text-gray-400 font-mono">{g.key}</span>
            <div className="flex-1" />
            <button
              onClick={() =>
                updateGenre.mutate(
                  { id: g.id, priority: g.priority - 1 },
                  { onSuccess: (res) => setLastReclassified(res.reclassified) },
                )
              }
              disabled={updateGenre.isPending}
              className="text-xs px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-50"
              title="優先順位を上げる（複数ジャンルに当たったとき勝ちやすくなる）"
            >
              ↑
            </button>
            <button
              onClick={() =>
                updateGenre.mutate(
                  { id: g.id, priority: g.priority + 1 },
                  { onSuccess: (res) => setLastReclassified(res.reclassified) },
                )
              }
              disabled={updateGenre.isPending}
              className="text-xs px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-50"
              title="優先順位を下げる"
            >
              ↓
            </button>
            <button
              onClick={() => {
                if (!confirm(`ジャンル「${g.label_ja}」を削除しますか？\n所属タグの割り当ても消え、記事は再分類されます。`)) return;
                deleteGenre.mutate(g.id, {
                  onSuccess: (res) => {
                    setLastReclassified(res.reclassified);
                    // 表示中のジャンルを削除した場合、その絞り込みに取り残されないよう解除する
                    if (filters.genre === g.key) {
                      onFilterChange({ ...filters, genre: undefined });
                    }
                  },
                });
              }}
              disabled={deleteGenre.isPending}
              className="text-xs px-1.5 py-0.5 rounded border border-red-300 text-red-600 dark:border-red-700 dark:text-red-400 disabled:opacity-50"
            >
              削除
            </button>
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {g.rules.map((r) => (
              <span key={r.id} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-xs bg-gray-100 dark:bg-gray-800 rounded">
                {r.tag}
                <button
                  onClick={() => deleteRule.mutate(r.id, { onSuccess: (res) => setLastReclassified(res.reclassified) })}
                  className="text-gray-400 hover:text-red-500"
                >
                  ×
                </button>
              </span>
            ))}
            {g.generic_rules.map((r) => (
              <span
                key={r.id}
                className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-xs border border-dashed border-gray-400 rounded"
                title="汎用ルール: 他に手がかりが無いときだけ使う"
              >
                {r.tag}
                <button
                  onClick={() => deleteRule.mutate(r.id, { onSuccess: (res) => setLastReclassified(res.reclassified) })}
                  className="text-gray-400 hover:text-red-500"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        </div>
      ))}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const tag = newTag.trim().toLowerCase();
          if (!tag || effectiveRuleGenreId == null) return;
          createRule.mutate(
            { tag, genre_id: effectiveRuleGenreId, is_generic: newIsGeneric },
            {
              onSuccess: (res) => {
                setLastReclassified(res.reclassified);
                setNewTag('');
              },
              onError: (err) => {
                alert((err as Error).message);
              },
            },
          );
        }}
        className="mt-2 flex items-center gap-1.5 flex-wrap"
      >
        <select
          value={effectiveRuleGenreId ?? ''}
          onChange={(e) => setNewRuleGenreId(Number(e.target.value))}
          className="text-sm px-1 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
        >
          {genres?.map((g) => (
            <option key={g.id} value={g.id}>
              {g.label_ja}
            </option>
          ))}
        </select>
        <input
          value={newTag}
          onChange={(e) => setNewTag(e.target.value)}
          placeholder="タグ (英小文字)"
          className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
        />
        <label className="text-xs text-gray-500 flex items-center gap-1">
          <input type="checkbox" checked={newIsGeneric} onChange={(e) => setNewIsGeneric(e.target.checked)} />
          汎用
        </label>
        <button type="submit" disabled={createRule.isPending} className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50">
          追加
        </button>
      </form>
      {lastReclassified !== null && (
        <p className="mt-1 text-xs text-gray-500">{lastReclassified} 件を分類し直しました</p>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const key = newGenreKey.trim().toLowerCase();
          if (!key || !newGenreLabel.trim()) return;
          createGenre.mutate(
            { key, label_ja: newGenreLabel.trim(), priority: DEFAULT_NEW_GENRE_PRIORITY },
            {
              onSuccess: (res) => {
                setNewGenreKey('');
                setNewGenreLabel('');
                setLastReclassified(res.reclassified);
              },
              onError: (err) => {
                alert((err as Error).message);
              },
            },
          );
        }}
        className="mt-3 flex items-center gap-1.5"
      >
        <input
          value={newGenreKey}
          onChange={(e) => setNewGenreKey(e.target.value)}
          placeholder="key (英小文字)"
          className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600 w-32"
        />
        <input
          value={newGenreLabel}
          onChange={(e) => setNewGenreLabel(e.target.value)}
          placeholder="表示名"
          className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600 w-32"
        />
        <button type="submit" className="text-xs text-blue-500 hover:text-blue-700">
          ジャンル追加
        </button>
      </form>

      <button onClick={onNavigateToOther} className="mt-2 text-xs text-blue-500 hover:text-blue-700">
        分類できなかった記事（その他）を見る
      </button>
    </div>
  );
}
