import { useState } from 'react';
import { ModalShell } from '../common/ModalShell';
import { useTags, useTagBulkStatus, useRenameTag, useBulkDeleteTags, useAiTagSaved, useAutoTagSaved, useFillTagTranslations } from '../../hooks/useTags';

interface Props {
  // 表示名の言語はアプリ全体の設定。サイドバー見出しのトグルと同じ状態を切り替える
  tagLang: 'en' | 'ja';
  onToggleTagLang: () => void;
  onClose: () => void;
}

// タグの改名・削除と、Saved 記事への一括タグ付けをまとめたモーダル。
// ジャンル管理と同じ ModalShell に載せることで、見出しの ⚙ から開くものは
// どれも同じ形で開く（サイドバー内にインラインで開くと一覧が押し下がる）。
export function TagManagerModal({ tagLang, onToggleTagLang, onClose }: Props) {
  const { data: tags } = useTags();
  const { data: bulkStatus } = useTagBulkStatus();
  const renameTag = useRenameTag();
  const bulkDeleteTags = useBulkDeleteTags();
  const aiTagSaved = useAiTagSaved();
  const autoTagSaved = useAutoTagSaved();
  const fillTranslations = useFillTagTranslations();

  const [editingTagId, setEditingTagId] = useState<number | null>(null);
  const [editingTagName, setEditingTagName] = useState('');

  const handleRenameSubmit = (tagId: number) => {
    const newName = editingTagName.trim();
    if (!newName) { setEditingTagId(null); return; }
    const lower = newName.toLowerCase();
    const existing = tags?.find(t =>
      t.id !== tagId && (
        t.name.toLowerCase() === lower ||
        (t.name_ja && t.name_ja === newName)
      )
    );
    if (existing && !confirm(`Tag "#${existing.name}" already exists — merge into it?`)) return;
    renameTag.mutate({ id: tagId, name: newName }, {
      onSuccess: () => setEditingTagId(null),
    });
  };

  return (
    <ModalShell title="タグ管理" onClose={onClose} maxWidthClass="max-w-2xl">
      <div className="flex items-center justify-between gap-2 pb-2">
        <div className="flex items-center gap-2">
          {/* 一覧の表示名を切り替える。サイドバー見出しのトグルと同じ状態 */}
          <button
            onClick={onToggleTagLang}
            className="flex items-center gap-0.5 rounded border border-gray-300 px-1.5 py-0.5 text-xs leading-none hover:border-gray-400 dark:border-gray-600"
            title="表示名の言語を切り替える"
          >
            <span className={tagLang === 'en' ? 'font-bold text-gray-700 dark:text-gray-200' : 'text-gray-400'}>EN</span>
            <span className="text-gray-300 dark:text-gray-600">|</span>
            <span className={tagLang === 'ja' ? 'font-bold text-gray-700 dark:text-gray-200' : 'text-gray-400'}>JA</span>
          </button>
          <span className="text-xs text-gray-400">{tags?.length ?? 0} タグ</span>
        </div>
      </div>

      {/* ラベルは「何に・何をする」の形に揃える。対象が Saved 記事に限られること、
          既存タグだけを使うのか AI が新しいタグを作るのかが、ラベルだけで分かるように。
          その下の対象件数は、仕事が無いのに押せるボタンが「壊れている」ように見えるのを防ぐ */}
      <div className="grid gap-2 border-y border-gray-200 py-2 dark:border-gray-700 sm:grid-cols-3">
        <BulkAction
          label="タグの日本語名を補完"
          target={bulkStatus && `未翻訳 ${bulkStatus.untranslated_tags} タグ`}
          idle={bulkStatus?.untranslated_tags === 0}
          status={fillTranslations.isPending ? '実行中...' : fillTranslations.isSuccess ? '完了' : null}
          pending={fillTranslations.isPending}
          onClick={() => fillTranslations.mutate()}
        />
        <BulkAction
          label="Saved に既存タグ付け"
          target={bulkStatus && `対象 ${bulkStatus.keyword_targets} 記事`}
          idle={bulkStatus?.keyword_targets === 0}
          status={
            autoTagSaved.isPending
              ? '照合中...'
              : autoTagSaved.isSuccess
                ? `${autoTagSaved.data.processed} 記事に ${autoTagSaved.data.attached} 件付与`
                : null
          }
          pending={autoTagSaved.isPending}
          onClick={() => autoTagSaved.mutate()}
        />
        <BulkAction
          label="Saved に AI タグ付け"
          target={bulkStatus && `対象 ${bulkStatus.ai_targets} 記事`}
          idle={bulkStatus?.ai_targets === 0}
          status={
            aiTagSaved.isPending
              ? '投入中...'
              : aiTagSaved.isSuccess
                ? `${aiTagSaved.data.queued} 記事を投入（残り ${aiTagSaved.data.remaining}）`
                : null
          }
          pending={aiTagSaved.isPending}
          onClick={() => aiTagSaved.mutate()}
        />
      </div>

      {/* 1 行が短いので、広くなった幅は列数に使う */}
      <div className="mt-2 grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
        {tags?.map((tag) => (
          <div key={tag.id} className="flex items-center gap-1">
            {editingTagId === tag.id ? (
              <form
                className="flex flex-1 gap-1"
                onSubmit={(e) => { e.preventDefault(); handleRenameSubmit(tag.id); }}
              >
                <input
                  type="text"
                  value={editingTagName}
                  onChange={(e) => setEditingTagName(e.target.value)}
                  className="flex-1 rounded border px-1.5 py-0.5 text-xs dark:border-gray-600 dark:bg-gray-800"
                  autoFocus
                  // Escape はモーダルを閉じる前に編集の取り消しに使う
                  onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setEditingTagId(null); } }}
                />
                <button type="submit" className="text-xs text-blue-500 hover:text-blue-700">✓</button>
                <button type="button" onClick={() => setEditingTagId(null)} className="text-xs text-gray-400 hover:text-gray-600">✕</button>
              </form>
            ) : (
              <>
                <span className="flex-1 truncate text-xs text-gray-600 dark:text-gray-400">
                  #{tagLang === 'ja' && tag.name_ja ? tag.name_ja : tag.name}
                </span>
                <button
                  onClick={() => { setEditingTagId(tag.id); setEditingTagName(tag.name); }}
                  className="px-1 text-sm leading-none text-gray-400 hover:text-blue-500"
                  title="Rename"
                >
                  ✏
                </button>
                <button
                  onClick={() => { if (confirm(`Delete tag "${tag.name}"?`)) bulkDeleteTags.mutate([tag.id]); }}
                  className="px-1 text-sm leading-none text-gray-400 hover:text-red-500"
                  title="Delete"
                >
                  ×
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </ModalShell>
  );
}

/** 一括操作 1 つ分。ラベルの下に対象件数を出し、実行後はそこに結果を出す。
    対象が 0 件なら押しても何も起きないので、その旨を出して無効にする */
function BulkAction({
  label, target, idle, status, pending, onClick,
}: {
  label: string;
  // 件数の取得前は undefined
  target: string | undefined | false;
  idle: boolean;
  status: string | null;
  pending: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={pending || (idle && !status)}
      className="rounded border border-gray-300 px-2 py-1.5 text-left hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
    >
      <span className="block text-xs font-medium text-gray-700 dark:text-gray-200">{label}</span>
      <span className={`mt-0.5 block text-[11px] leading-snug ${status ? 'text-green-600' : 'text-gray-400'}`}>
        {status ?? target ?? '…'}
      </span>
    </button>
  );
}
