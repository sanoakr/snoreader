import { useEffect } from 'react';
import { createPortal } from 'react-dom';

// 同時に 2 枚開いた場合に、先に閉じた側が body のフラグを消してしまわないよう数える
let openCount = 0;

interface Props {
  title: string;
  onClose: () => void;
  // 中身の想定幅。ジャンル辞書のように広い表は 3xl、タグ一覧のように短い行は 2xl で足りる
  maxWidthClass?: string;
  children: React.ReactNode;
}

// サイドバーの管理 UI（ジャンル辞書・タグ一覧）を載せる共通のオーバーレイ。
// 幅 256px のサイドバーにインラインで開くと横が溢れ、縦も一覧を押し下げてしまうため、
// body 直下に portal で描く。App.tsx がモバイルドロワー用にサイドバーを
// transition-transform の要素で包んでいるので、portal を使わないと fixed の基準がずれる。
export function ModalShell({ title, onClose, maxWidthClass = 'max-w-3xl', children }: Props) {
  // 開いている間は Escape で閉じ、ArticleList / ArticleReader の window ショートカット
  // （j/k/s/矢印/Space）を止める。あちらのガードは INPUT / TEXTAREA しか除外しないので、
  // 「モーダルが開いている」ことを body の data 属性で共有する
  useEffect(() => {
    openCount += 1;
    document.body.dataset.modalOpen = 'true';
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      openCount -= 1;
      if (openCount === 0) delete document.body.dataset.modalOpen;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 p-3 sm:p-6"
      // 背景そのものを押したときだけ閉じる（パネル内でのドラッグ終了で閉じないよう target を見る）
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className={`flex max-h-[calc(100vh-1.5rem)] w-full flex-col overflow-hidden rounded-lg bg-white shadow-xl dark:bg-gray-900 sm:max-h-[calc(100vh-3rem)] ${maxWidthClass}`}>
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">{title}</h2>
          <button
            onClick={onClose}
            title="閉じる"
            className="flex h-6 w-6 items-center justify-center rounded text-base leading-none text-gray-400 hover:bg-gray-200 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          >
            ✕
          </button>
        </div>
        <div className="overflow-y-auto px-4 py-3">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
