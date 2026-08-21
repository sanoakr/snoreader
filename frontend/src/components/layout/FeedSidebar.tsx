import { useRef, useState } from 'react';
import { Spinner } from '../common/Spinner';
import { useFeeds, useCreateFeed, useDeleteFeed, useRefreshFeed, useImportOpml, useImportArticles, useDedupArticles } from '../../hooks/useFeeds';
import { useRecommendedCount, useSavedCount, useAiStatus, useExtractFailed, useGenreCounts } from '../../hooks/useArticles';
import { useTags } from '../../hooks/useTags';
import { useExcludePatterns, useCreateExcludePattern, useDeleteExcludePattern } from '../../hooks/useExcludePatterns';
import { opmlExportUrl, savedArticlesExportUrl } from '../../api/client';
import { GenreManagerModal } from './GenreManagerModal';
import { TagManagerModal } from './TagManagerModal';
import { useSplitSuggestions } from '../../hooks/useSplitSuggestions';
import type { ArticleFilters } from '../../types';

// 親の未読がこれを超えたらサイドバーで子ジャンルを展開する。
// 「これだけなら片付けられる」と思える大きさに束を割るための境界
const GENRE_SPLIT_THRESHOLD = 30;

interface Props {
  filters: ArticleFilters;
  onFilterChange: (f: ArticleFilters) => void;
  tagLang: 'en' | 'ja';
  onToggleTagLang: () => void;
  darkToggle?: React.ReactNode;
}

export function FeedSidebar({ filters, onFilterChange, tagLang, onToggleTagLang, darkToggle }: Props) {
  const { data: feeds, isLoading } = useFeeds();
  const { data: tags } = useTags();
  const createFeed = useCreateFeed();
  const deleteFeed = useDeleteFeed();
  const refreshFeed = useRefreshFeed();
  const importOpml = useImportOpml();
  const importArticles = useImportArticles();
  const dedupArticles = useDedupArticles();
  const { data: excludePatterns } = useExcludePatterns();
  const createExcludePattern = useCreateExcludePattern();
  const deleteExcludePattern = useDeleteExcludePattern();
  const [newUrl, setNewUrl] = useState('');
  const [showAdd, setShowAdd] = useState(false);
  const [showTagManager, setShowTagManager] = useState(false);
  const [excludeManageMode, setExcludeManageMode] = useState(false);
  const [newExcludePattern, setNewExcludePattern] = useState('');
  const [showGenreManager, setShowGenreManager] = useState(false);
  const [feedToolsOpen, setFeedToolsOpen] = useState(false);
  const opmlFileRef = useRef<HTMLInputElement>(null);
  const articlesFileRef = useRef<HTMLInputElement>(null);

  const totalUnread = feeds?.reduce((s, f) => s + f.unread_count, 0) ?? 0;
  const { data: recommendedCount } = useRecommendedCount();
  const { data: savedCount } = useSavedCount();
  const { data: aiStatus } = useAiStatus();
  const { data: extractFailed } = useExtractFailed();
  const extractFailedCount = extractFailed?.length ?? 0;
  const { data: genreCounts } = useGenreCounts();
  const { data: splitSuggestions } = useSplitSuggestions();
  const pendingSplits = splitSuggestions?.length ?? 0;
  // 残件のある種別だけを並べる（3 種すべてを条件式で繋ぐと区切りの制御が破綻するため）
  const aiPendingLabel = [
    aiStatus?.pending_summary ? `要約 ${aiStatus.pending_summary}件` : null,
    aiStatus?.pending_tags ? `タグ ${aiStatus.pending_tags}件` : null,
    aiStatus?.pending_questions ? `質問候補 ${aiStatus.pending_questions}件` : null,
  ]
    .filter(Boolean)
    .join(' / ');

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newUrl.trim()) return;
    await createFeed.mutateAsync(newUrl.trim());
    setNewUrl('');
    setShowAdd(false);
  };

  const handleOpmlImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) importOpml.mutate(file);
    e.target.value = '';
  };

  const handleArticlesImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) importArticles.mutate(file);
    e.target.value = '';
  };

  const handleDedup = async () => {
    const preview = await dedupArticles.mutateAsync(true);
    if (preview.deleted === 0) {
      alert('重複記事はありません');
      return;
    }
    const ok = confirm(
      `重複記事 ${preview.deleted} 件（${preview.duplicate_groups} グループ）を削除します。よろしいですか?`
    );
    if (!ok) return;
    await dedupArticles.mutateAsync(false);
  };

  const handleAddExcludePattern = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newExcludePattern.trim()) return;
    const result = await createExcludePattern.mutateAsync(newExcludePattern.trim());
    setNewExcludePattern('');
    if (result.purged > 0) {
      alert(`既存の一致記事 ${result.purged} 件を削除しました`);
    }
  };

  // ビューは filters の排他フラグで表せているので、ジャンルを選ぶときは他を明示的に消す
  const selectGenre = (genre: string, opts?: { exact?: boolean }) => {
    onFilterChange({
      ...filters,
      genre,
      genre_exact: opts?.exact ? true : undefined,
      dismissed: undefined, feed_id: undefined, is_saved: undefined,
      tag_id: undefined, untagged: undefined,
      recommended: undefined, unrecommended: undefined, extract_failed: undefined,
    });
  };

  return (
    <aside className="w-64 shrink-0 border-r border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 h-screen overflow-y-auto flex flex-col">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100">SnoReader</h1>
        {darkToggle}
      </div>

      <nav className="flex-1 p-2 space-y-0.5">
        {/* All articles */}
        <button
          onClick={() => onFilterChange({ ...filters, genre: undefined, genre_exact: undefined, dismissed: undefined, feed_id: undefined, is_saved: undefined, tag_id: undefined, untagged: undefined, recommended: undefined, unrecommended: undefined, extract_failed: undefined })}
          className={`w-full text-left px-3 py-2 rounded text-sm flex justify-between items-center hover:bg-gray-200 dark:hover:bg-gray-800 ${
            filters.feed_id == null && filters.is_saved == null && filters.tag_id == null && !filters.untagged && !filters.recommended && !filters.unrecommended && !filters.extract_failed && filters.genre == null && !filters.dismissed ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
          }`}
        >
          <span>All</span>
          {totalUnread > 0 && (
            <span className="text-xs bg-blue-500 text-white rounded-full px-1.5 py-0.5 min-w-[20px] text-center">
              {totalUnread}
            </span>
          )}
        </button>

        {/* Recommended */}
        <button
          onClick={() => onFilterChange({ recommended: true })}
          className={`w-full text-left px-3 py-2 rounded text-sm flex justify-between items-center hover:bg-gray-200 dark:hover:bg-gray-800 ${
            filters.recommended ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
          }`}
        >
          <span>✦ Recommend</span>
          {!!recommendedCount && (
            <span className="text-xs bg-blue-500 text-white rounded-full px-1.5 py-0.5 min-w-[20px] text-center">
              {recommendedCount}
            </span>
          )}
        </button>

        {/* Saved */}
        <button
          onClick={() => onFilterChange({ ...filters, genre: undefined, genre_exact: undefined, dismissed: undefined, feed_id: undefined, is_saved: true, is_read: undefined, tag_id: undefined, untagged: undefined, recommended: undefined, unrecommended: undefined, extract_failed: undefined })}
          className={`w-full text-left px-3 py-2 rounded text-sm flex justify-between items-center hover:bg-gray-200 dark:hover:bg-gray-800 ${
            filters.is_saved === true && filters.tag_id == null && !filters.untagged ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
          }`}
        >
          <span>★ Saved</span>
          {!!savedCount && (
            <span className="text-xs text-gray-400 tabular-nums">{savedCount}</span>
          )}
        </button>

        {/* Saved 記事の入出力は、対象である Saved ビューの直下に置く */}
        <div className="flex gap-1 px-1">
          <button
            onClick={() => articlesFileRef.current?.click()}
            disabled={importArticles.isPending}
            className="flex-1 px-2 py-1 text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800 rounded disabled:opacity-50"
          >
            {importArticles.isPending ? 'Importing...' : 'Import Saved'}
          </button>
          <a
            href={savedArticlesExportUrl}
            download
            className="flex-1 px-2 py-1 text-xs text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800 rounded text-center"
          >
            Export Saved
          </a>
        </div>
        {importArticles.isSuccess && (
          <p className="px-2 text-xs text-green-600">
            Imported {importArticles.data.articles_created} articles, {importArticles.data.feeds_created} feeds
          </p>
        )}
        {importArticles.isError && (
          <p className="px-2 text-xs text-red-500">{(importArticles.error as Error).message}</p>
        )}

        {/* タグの絞り込みチップは Saved ビュー側にあるので、ここは表示言語の切り替えと
            管理モーダルへの入口だけを持つ */}
        {tags && tags.length > 0 && (
          <>
            <hr className="my-2 border-gray-200 dark:border-gray-700" />
            <SectionHeading label="タグ">
              <button
                onClick={onToggleTagLang}
                className="flex items-center text-xs gap-0.5 px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600 hover:border-gray-400 leading-none"
                title="Toggle tag language"
              >
                <span className={tagLang === 'en' ? 'font-bold text-gray-700 dark:text-gray-200' : 'text-gray-400'}>EN</span>
                <span className="text-gray-300 dark:text-gray-600">|</span>
                <span className={tagLang === 'ja' ? 'font-bold text-gray-700 dark:text-gray-200' : 'text-gray-400'}>JA</span>
              </button>
              <IconButton
                label="⚙"
                title="タグ管理（名前の変更・削除・一括タグ付け）"
                active={showTagManager}
                onClick={() => setShowTagManager(m => !m)}
              />
            </SectionHeading>
            {showTagManager && (
              <TagManagerModal tagLang={tagLang} onClose={() => setShowTagManager(false)} />
            )}
          </>
        )}

        <hr className="my-2 border-gray-200 dark:border-gray-700" />

        {/* ジャンル別ナビゲーション。件数 0 のジャンルは API が返さないのでそのまま並べる。
            親の未読が閾値を超えたときだけ子を展開する（超えていなければ従来通り 1 行） */}
        <div className="mt-4">
          <SectionHeading label="ジャンル">
            <IconButton
              label="⚙"
              title="ジャンル管理（タグの割り当てと優先順位）"
              active={showGenreManager}
              badge={pendingSplits}
              badgeTitle={`未読が上限を超えたジャンルの分割提案が ${pendingSplits} 件あります`}
              onClick={() => setShowGenreManager(m => !m)}
            />
          </SectionHeading>
          {showGenreManager && (
            <GenreManagerModal
              filters={filters}
              onFilterChange={onFilterChange}
              onClose={() => setShowGenreManager(false)}
              onNavigateToOther={() => {
                setShowGenreManager(false);
                onFilterChange({ ...filters, genre: 'other', genre_exact: undefined, dismissed: undefined });
              }}
            />
          )}
          {genreCounts?.map((g) => {
              // 表示中の束が閾値を割った瞬間に選択中の行が消えないよう、
              // その親配下を見ている間は件数に関わらず展開したままにする
              const viewingHere =
                filters.genre === g.genre || g.children.some((c) => c.genre === filters.genre);
              const expanded =
                g.children.length > 0 &&
                (g.unread_count > GENRE_SPLIT_THRESHOLD || viewingHere);
              return (
                <div key={g.genre}>
                  <GenreNavRow
                    label={g.label_ja}
                    count={g.unread_count}
                    active={filters.genre === g.genre && !filters.genre_exact}
                    // 子を展開している親行は集計の見出しなので、超過していても警告色にしない
                    warn={!expanded && g.unread_count > GENRE_SPLIT_THRESHOLD}
                    onClick={() => selectGenre(g.genre)}
                  />
                  {expanded && (
                    <>
                      {g.children.map((c) => (
                        <GenreNavRow
                          key={c.genre}
                          label={c.label_ja}
                          count={c.unread_count}
                          indent
                          active={filters.genre === c.genre}
                          warn={c.unread_count > GENRE_SPLIT_THRESHOLD}
                          onClick={() => selectGenre(c.genre)}
                        />
                      ))}
                      {(g.direct_count > 0 || (filters.genre === g.genre && !!filters.genre_exact)) && (
                        <GenreNavRow
                          label="その他"
                          count={g.direct_count}
                          indent
                          active={filters.genre === g.genre && !!filters.genre_exact}
                          warn={g.direct_count > GENRE_SPLIT_THRESHOLD}
                          onClick={() => selectGenre(g.genre, { exact: true })}
                        />
                      )}
                    </>
                  )}
                </div>
              );
          })}

          {/* 非表示にした記事（ジャンルで束にした未読を「後回し」にした記事）の一覧 */}
          <button
            onClick={() => onFilterChange({
              ...filters, dismissed: true, genre: undefined, genre_exact: undefined,
              feed_id: undefined, is_saved: undefined, tag_id: undefined, untagged: undefined,
              recommended: undefined, unrecommended: undefined, extract_failed: undefined,
            })}
            className={`w-full px-2 py-1 text-sm text-left rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
              filters.dismissed ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
            }`}
          >
            非表示にした記事
          </button>
        </div>

        <hr className="my-2 border-gray-200 dark:border-gray-700" />

        {/* フィード一覧。フィードそのものへの操作は「何に対する操作か」の隣にあるべきなので、
            重複記事・除外パターン・OPML の入出力は見出しの ⚙ にまとめて畳んでおく */}
        <SectionHeading label="フィード">
          <IconButton
            label="⚙"
            title="フィードの設定（重複記事・除外パターン・OPML）"
            active={feedToolsOpen}
            onClick={() => { setFeedToolsOpen(o => !o); setExcludeManageMode(false); }}
          />
        </SectionHeading>
        {feedToolsOpen && (
          <div className="px-2 pb-1 space-y-1">
            <div className="flex gap-1">
              <button
                onClick={handleDedup}
                disabled={dedupArticles.isPending}
                className="flex-1 px-1 py-1.5 text-xs whitespace-nowrap text-gray-500 border border-gray-300 dark:border-gray-600 hover:bg-gray-200 dark:hover:bg-gray-800 rounded disabled:opacity-50"
                title="フィード横断で同一URLの重複記事を検出し、はてなブックマーク由来を優先して削除する"
              >
                {dedupArticles.isPending ? '確認中...' : '重複記事を整理'}
              </button>
              <button
                onClick={() => setExcludeManageMode(m => !m)}
                className={`flex-1 px-1 py-1.5 text-xs whitespace-nowrap rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-200 dark:hover:bg-gray-800 ${
                  excludeManageMode ? 'bg-gray-200 dark:bg-gray-800 text-gray-700 dark:text-gray-200' : 'text-gray-500'
                }`}
                title="URLパターンに一致する記事をフェッチ時にスキップする"
              >
                除外パターン管理
              </button>
            </div>
            {dedupArticles.isSuccess && dedupArticles.data.dry_run === false && (
              <p className="text-xs text-green-600">
                重複記事 {dedupArticles.data.deleted} 件を削除しました
              </p>
            )}
            {dedupArticles.isError && (
              <p className="text-xs text-red-500">{(dedupArticles.error as Error).message}</p>
            )}
            {/* 購読リストの入出力もフィードの設定。読む操作ではないので同じ ⚙ の中に畳む */}
            <div className="flex gap-1">
              <button
                onClick={() => opmlFileRef.current?.click()}
                disabled={importOpml.isPending}
                className="flex-1 px-1 py-1.5 text-xs whitespace-nowrap text-gray-500 border border-gray-300 dark:border-gray-600 hover:bg-gray-200 dark:hover:bg-gray-800 rounded disabled:opacity-50"
              >
                {importOpml.isPending ? 'Importing...' : 'Import OPML'}
              </button>
              <a
                href={opmlExportUrl}
                download
                className="flex-1 px-1 py-1.5 text-xs whitespace-nowrap text-center text-gray-500 border border-gray-300 dark:border-gray-600 hover:bg-gray-200 dark:hover:bg-gray-800 rounded"
              >
                Export OPML
              </a>
            </div>
            {importOpml.isSuccess && (
              <p className="text-xs text-green-600">
                Imported {importOpml.data.created} feeds ({importOpml.data.skipped} skipped)
              </p>
            )}
            {excludeManageMode && (
              <div className="space-y-1">
                {excludePatterns?.map((p) => (
                  <div key={p.id} className="flex items-center gap-1 group">
                    <span className="flex-1 text-xs text-gray-600 dark:text-gray-400 truncate">{p.pattern}</span>
                    <button
                      onClick={() => { if (confirm(`パターン "${p.pattern}" を削除しますか?`)) deleteExcludePattern.mutate(p.id); }}
                      className="text-gray-400 hover:text-red-500 text-sm px-1 leading-none"
                      title="Delete"
                    >
                      ×
                    </button>
                  </div>
                ))}
                <form onSubmit={handleAddExcludePattern} className="flex gap-1">
                  <input
                    type="text"
                    value={newExcludePattern}
                    onChange={(e) => setNewExcludePattern(e.target.value)}
                    placeholder="例: tonarinoyj.jp/episode/*"
                    className="flex-1 min-w-0 px-1.5 py-0.5 text-xs border rounded dark:bg-gray-800 dark:border-gray-600"
                  />
                  <button
                    type="submit"
                    disabled={createExcludePattern.isPending}
                    className="text-xs px-2 py-0.5 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
                  >
                    追加
                  </button>
                </form>
                {createExcludePattern.isError && (
                  <p className="text-xs text-red-500">{(createExcludePattern.error as Error).message}</p>
                )}
              </div>
            )}
          </div>
        )}
        {isLoading && <div className="flex justify-center py-3"><Spinner size="sm" /></div>}
        {feeds?.map((feed) => (
          <div key={feed.id} className="group flex items-center">
            <button
              onClick={() => onFilterChange({ ...filters, genre: undefined, genre_exact: undefined, dismissed: undefined, feed_id: feed.id, is_saved: undefined, tag_id: undefined, untagged: undefined, recommended: undefined, unrecommended: undefined, extract_failed: undefined })}
              className={`flex-1 text-left px-3 py-1.5 rounded text-sm truncate flex items-center gap-2 hover:bg-gray-200 dark:hover:bg-gray-800 ${
                filters.feed_id === feed.id ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
              }`}
            >
              {feed.favicon_url ? (
                <img src={feed.favicon_url} alt="" className="w-4 h-4 shrink-0 rounded" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
              ) : (
                <span className="w-4 h-4 shrink-0 rounded bg-gray-300 dark:bg-gray-600 text-[10px] flex items-center justify-center text-gray-500 dark:text-gray-400">
                  {(feed.title || feed.url)[0]?.toUpperCase()}
                </span>
              )}
              <span className="truncate flex-1">{feed.title || feed.url}</span>
              {feed.unread_count > 0 && (
                <span className="text-xs bg-blue-500 text-white rounded-full px-1.5 py-0.5 min-w-[20px] text-center shrink-0">{feed.unread_count}</span>
              )}
            </button>
            <div className="hidden group-hover:flex items-center gap-0.5 pr-1">
              <button
                onClick={() => refreshFeed.mutate(feed.id)}
                className="text-gray-400 hover:text-blue-500 p-0.5"
                title="Refresh"
              >
                ↻
              </button>
              <button
                onClick={() => { if (confirm(`Delete "${feed.title || feed.url}"?`)) deleteFeed.mutate(feed.id); }}
                className="text-gray-400 hover:text-red-500 p-0.5"
                title="Delete"
              >
                ×
              </button>
            </div>
          </div>
        ))}

        {/* フィードの追加は一覧の続きに置く（一覧を見た流れで足せるように） */}
        {showAdd ? (
          <form onSubmit={handleAdd} className="px-1 pt-1 space-y-2">
            <input
              type="url"
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              placeholder="https://example.com/feed.xml"
              className="w-full px-2 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-600"
              autoFocus
            />
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={createFeed.isPending}
                className="flex-1 px-2 py-1 text-sm bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
              >
                {createFeed.isPending ? 'Adding...' : 'Add'}
              </button>
              <button
                type="button"
                onClick={() => setShowAdd(false)}
                className="px-2 py-1 text-sm border rounded hover:bg-gray-100 dark:hover:bg-gray-800"
              >
                Cancel
              </button>
            </div>
            {createFeed.isError && (
              <p className="text-xs text-red-500">{(createFeed.error as Error).message}</p>
            )}
          </form>
        ) : (
          <button
            onClick={() => setShowAdd(true)}
            className="w-full px-3 py-2 text-left text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-800 rounded"
          >
            + Add Feed
          </button>
        )}
      </nav>

      {/* 下段は常に見えていてほしい状態表示だけを残す */}
      <div className="p-2 border-t border-gray-200 dark:border-gray-700 space-y-1">
        <input ref={opmlFileRef} type="file" accept=".opml,.xml" onChange={handleOpmlImport} className="hidden" />
        <input ref={articlesFileRef} type="file" accept=".json" onChange={handleArticlesImport} className="hidden" />
        {aiStatus && aiPendingLabel && (
          <div className="px-1 py-1.5 flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
            {aiStatus.available && <Spinner size="sm" />}
            <span>
              {aiStatus.available ? 'AI処理中' : 'AI待機中'}{' — '}
              {aiPendingLabel}
            </span>
          </div>
        )}
        {extractFailedCount > 0 && (
          <button
            onClick={() => onFilterChange({ extract_failed: true })}
            className={`w-full px-2 py-1.5 text-xs rounded flex justify-between items-center ${
              filters.extract_failed
                ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 font-semibold'
                : 'text-amber-600 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20'
            }`}
            title="本文取得に失敗した記事を確認・対処"
          >
            <span>⚠ 取得失敗</span>
            <span>{extractFailedCount} 件</span>
          </button>
        )}
      </div>
    </aside>
  );
}

/** ジャンルナビの 1 行。選べる束が閾値を超えていたら件数バッジを警告色にする */
function GenreNavRow({
  label, count, active, warn, indent, onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  warn: boolean;
  indent?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      title={warn ? '子ジャンルを追加すると更に分けられます' : undefined}
      className={`w-full flex items-center gap-2 py-1 text-sm text-left rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
        indent ? 'pl-6 pr-2' : 'px-2'
      } ${active ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''}`}
    >
      <span className="truncate flex-1">{indent ? `↳ ${label}` : label}</span>
      <span
        className={`text-xs rounded-full px-1.5 py-0.5 min-w-[20px] text-center shrink-0 text-white ${
          warn ? 'bg-amber-500' : 'bg-blue-500'
        }`}
      >
        {count}
      </span>
    </button>
  );
}

/** タグ / ジャンル / フィードの見出し行。管理操作は右端の ⚙ に集約する */
function SectionHeading({ label, children }: { label: string; children?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 px-2 pt-1 pb-0.5">
      <span className="text-xs font-semibold text-gray-400">{label}</span>
      <div className="flex items-center gap-1">{children}</div>
    </div>
  );
}

/** 見出し行の管理ボタン。12px の裸の記号では何のマークか読めなかったので、
    16px の記号と 24px の当たり判定を確保する。開閉は文字の差し替えではなく
    背景で示す（ラベルの幅が変わると見出し行が揺れるため） */
function IconButton({
  label, title, active, onClick, badge, badgeTitle,
}: {
  label: string;
  title: string;
  active?: boolean;
  onClick: () => void;
  badge?: number;
  badgeTitle?: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`relative flex h-6 w-6 shrink-0 items-center justify-center rounded text-base leading-none ${
        active
          ? 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
          : 'text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'
      }`}
    >
      {label}
      {!!badge && (
        <span
          className="absolute -top-0.5 -right-0.5 rounded-full bg-amber-500 px-1 text-[10px] font-medium leading-tight text-white"
          title={badgeTitle}
        >
          {badge}
        </span>
      )}
    </button>
  );
}
