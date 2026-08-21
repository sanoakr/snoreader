import { useState } from 'react';
import { ModalShell } from '../common/ModalShell';
import { useTags, useRenameTag, useBulkDeleteTags, useAiTagSaved, useAutoTagSaved, useFillTagTranslations } from '../../hooks/useTags';

interface Props {
  // 表示名の言語はサイドバーの EN|JA トグルと共有する（表示設定なのでモーダル側には置かない）
  tagLang: 'en' | 'ja';
  onClose: () => void;
}

// タグの改名・削除と、Saved 記事への一括タグ付けをまとめたモーダル。
// ジャンル管理と同じ ModalShell に載せることで、見出しの ⚙ から開くものは
// どれも同じ形で開く（サイドバー内にインラインで開くと一覧が押し下がる）。
export function TagManagerModal({ tagLang, onClose }: Props) {
  const { data: tags } = useTags();
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
      <div className="flex flex-wrap items-center gap-3 border-b border-gray-200 pb-2 dark:border-gray-700">
        <button
          onClick={() => fillTranslations.mutate()}
          disabled={fillTranslations.isPending}
          className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50"
          title="Translate English tags to Japanese"
        >
          {fillTranslations.isPending ? 'Translating...' : fillTranslations.isSuccess ? 'Done' : 'JA補完'}
        </button>
        <button
          onClick={() => autoTagSaved.mutate()}
          disabled={autoTagSaved.isPending}
          className="text-xs text-green-600 hover:text-green-700 disabled:opacity-50"
          title="Auto-tag untagged Saved articles via existing-tag keyword match"
        >
          {autoTagSaved.isPending ? 'Matching...' : autoTagSaved.isSuccess ? `+${autoTagSaved.data.attached} on ${autoTagSaved.data.processed}` : 'Auto tag'}
        </button>
        <button
          onClick={() => aiTagSaved.mutate()}
          disabled={aiTagSaved.isPending}
          className="text-xs text-purple-500 hover:text-purple-700 disabled:opacity-50"
          title="AI tag Saved articles (10 at a time)"
        >
          {aiTagSaved.isPending ? 'AI...' : aiTagSaved.isSuccess ? `+${aiTagSaved.data.queued} (${aiTagSaved.data.remaining} left)` : 'AI tag'}
        </button>
        {!!tags && <span className="ml-auto text-xs text-gray-400">{tags.length} tags</span>}
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
