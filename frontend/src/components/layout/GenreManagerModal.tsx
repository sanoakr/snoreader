import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  useGenres,
  useCreateGenre,
  useUpdateGenre,
  useDeleteGenre,
  useCreateGenreRule,
  useDeleteGenreRule,
  useSeedSubgenres,
} from '../../hooks/useGenres';
import { SplitSuggestionPanel } from './SplitSuggestionPanel';
import type { ArticleFilters, GenreDef } from '../../types';

// 新規ジャンルの初期優先度。数字が小さいほど分類時に優先される。
const DEFAULT_NEW_GENRE_PRIORITY = 100;

interface Props {
  // 「分類できなかった記事（その他）を見る」導線用。呼び出し元でモーダルを閉じて filter を切り替える。
  onNavigateToOther: () => void;
  // 背景クリック・✕・Escape の 3 経路から閉じる
  onClose: () => void;
  // 削除したジャンルが現在表示中のビューと同じ場合に、その絞り込みから抜けるために必要
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
}

// ジャンル辞書（タグ→ジャンルの割り当て、優先順位）を編集するモーダル。
// サイドバー幅 256px にインラインで開くと横も縦も溢れるため、body 直下のオーバーレイとして描く
// （サイドバーの外側が transition-transform を持つので、portal を使わないと fixed の基準がずれる）。
export function GenreManagerModal({ onNavigateToOther, onClose, filters, onFilterChange }: Props) {
  const { data: genres } = useGenres();
  const createGenre = useCreateGenre();
  const updateGenre = useUpdateGenre();
  const deleteGenre = useDeleteGenre();
  const createRule = useCreateGenreRule();
  const deleteRule = useDeleteGenreRule();
  const seedSubgenres = useSeedSubgenres();

  const [newTag, setNewTag] = useState('');
  const [newRuleGenreId, setNewRuleGenreId] = useState<number | null>(null);
  const [newIsGeneric, setNewIsGeneric] = useState(false);
  const [lastReclassified, setLastReclassified] = useState<number | null>(null);
  const [newGenreKey, setNewGenreKey] = useState('');
  const [newGenreLabel, setNewGenreLabel] = useState('');
  const [newGenreParentId, setNewGenreParentId] = useState<number | null>(null);

  // 開いている間は Escape で閉じ、ArticleList / ArticleReader の window ショートカット
  // （j/k/s/矢印/Space）を止める。あちらのガードは INPUT / TEXTAREA しか除外しないので、
  // 「モーダルが開いている」ことを body の data 属性で共有する
  useEffect(() => {
    document.body.dataset.modalOpen = 'true';
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      delete document.body.dataset.modalOpen;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  // タグ追加フォームの <select> は空の選択肢を持たないため、ユーザーがまだ選択していない間は
  // 先頭のジャンルを既定値として使う（state 自体は未選択のまま保つ。setState-in-effect を避けるため）。
  const effectiveRuleGenreId = newRuleGenreId ?? genres?.[0]?.id ?? null;

  // 親→子の入れ子で並べる。priority 昇順は既存のまま
  const tree = useMemo(() => {
    const list = genres ?? [];
    const parents = list.filter((g) => g.parent_id == null);
    return parents.map((p) => ({
      parent: p,
      children: list.filter((c) => c.parent_id === p.id),
    }));
  }, [genres]);

  // 親行と子行で描画は共通。子は親と同じ priority を保つのが分割の前提なので上下ボタンを出さない
  const renderGenre = (g: GenreDef, isChild: boolean) => (
    <div
      key={g.id}
      className={`border-b border-gray-200 dark:border-gray-700 py-2 ${isChild ? 'pl-6' : ''}`}
    >
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
        {!isChild && (
          <>
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
          </>
        )}
        <button
          onClick={() => {
            if (!confirm(`ジャンル「${g.label_ja}」を削除しますか？\n所属タグの割り当ても消え、記事は再分類されます。`)) return;
            deleteGenre.mutate(g.id, {
              onSuccess: (res) => {
                setLastReclassified(res.reclassified);
                // 表示中のジャンルを削除した場合、その絞り込みに取り残されないよう解除する
                if (filters.genre === g.key) {
                  onFilterChange({ ...filters, genre: undefined, genre_exact: undefined });
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
  );

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 p-3 sm:p-6"
      // 背景そのものを押したときだけ閉じる（パネル内でのドラッグ終了で閉じないよう target を見る）
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="flex max-h-[calc(100vh-1.5rem)] w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-white shadow-xl dark:bg-gray-900 sm:max-h-[calc(100vh-3rem)]">
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">ジャンル管理</h2>
          <button
            onClick={onClose}
            title="閉じる"
            className="flex h-6 w-6 items-center justify-center rounded text-base leading-none text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          >
            ✕
          </button>
        </div>
        <div className="space-y-1 overflow-y-auto px-4 py-3">
          <SplitSuggestionPanel />
      {tree.map(({ parent, children }) => (
        <div key={parent.id}>
          {renderGenre(parent, false)}
          {children.map((c) => renderGenre(c, true))}
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
          {tree.map(({ parent, children }) => (
            <optgroup key={parent.id} label={parent.label_ja}>
              <option value={parent.id}>{parent.label_ja}</option>
              {children.map((c) => (
                <option key={c.id} value={c.id}>{`↳ ${c.label_ja}`}</option>
              ))}
            </optgroup>
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
          // 子は親と同じ priority を持つのが前提（祖先・子孫の枝刈りで子が勝ち、
          // 他ジャンルとの優劣は親のときと変わらない）
          const parent = (genres ?? []).find((g) => g.id === newGenreParentId);
          createGenre.mutate(
            {
              key,
              label_ja: newGenreLabel.trim(),
              priority: parent ? parent.priority : DEFAULT_NEW_GENRE_PRIORITY,
              parent_id: newGenreParentId,
            },
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
        <select
          value={newGenreParentId ?? ''}
          onChange={(e) => setNewGenreParentId(e.target.value ? Number(e.target.value) : null)}
          className="px-1.5 py-1 text-xs border rounded dark:bg-gray-800 dark:border-gray-600"
        >
          <option value="">親ジャンルとして作成</option>
          {(genres ?? []).filter((g) => g.parent_id == null).map((g) => (
            <option key={g.id} value={g.id}>{g.label_ja} の子</option>
          ))}
        </select>
        <button type="submit" className="text-xs text-blue-500 hover:text-blue-700">
          ジャンル追加
        </button>
      </form>

      <div className="mt-2 flex items-center gap-3">
        <button onClick={onNavigateToOther} className="text-xs text-blue-500 hover:text-blue-700">
          分類できなかった記事（その他）を見る
        </button>
        <button
          onClick={() => {
            if (!confirm('推奨サブジャンルを投入します。既存記事の再分類に十数秒かかります。')) return;
            seedSubgenres.mutate(undefined, {
              onSuccess: (r) => setLastReclassified(r.reclassified),
            });
          }}
          disabled={seedSubgenres.isPending}
          className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50"
        >
          {seedSubgenres.isPending ? '投入中...' : '推奨サブジャンルを投入'}
        </button>
        </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
