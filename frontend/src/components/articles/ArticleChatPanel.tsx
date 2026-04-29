import { useEffect, useRef, useState } from 'react';
import { useChatWithArticle } from '../../hooks/useArticles';
import { Spinner } from '../common/Spinner';
import type { ChatMessage } from '../../types';

interface Props {
  articleId: number;
}

export function ArticleChatPanel({ articleId }: Props) {
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const chat = useChatWithArticle();

  // 記事切替で履歴クリア
  useEffect(() => {
    setHistory([]);
    setInput('');
    setError(null);
  }, [articleId]);

  // 新着メッセージへ自動スクロール
  useEffect(() => {
    historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: 'smooth' });
  }, [history.length, chat.isPending]);

  const send = () => {
    const message = input.trim();
    if (!message || chat.isPending) return;
    const userMsg: ChatMessage = { role: 'user', content: message };
    const nextHistory = [...history, userMsg];
    setHistory(nextHistory);
    setInput('');
    setError(null);
    chat.mutate(
      { id: articleId, message, history },
      {
        onSuccess: (res) => {
          setHistory([...nextHistory, { role: 'assistant', content: res.message }]);
        },
        onError: (e) => {
          // 失敗したユーザー発言を履歴から取り除き、エラー表示
          setHistory(history);
          setInput(message);
          setError(e instanceof Error ? e.message : 'Chat failed');
        },
      },
    );
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="sticky bottom-0 bg-white dark:bg-gray-950 border-t border-gray-200 dark:border-gray-800">
      <div className="max-w-3xl mx-auto">
        {(history.length > 0 || chat.isPending || error) && (
          <div ref={historyRef} className="max-h-[40vh] overflow-y-auto p-3 space-y-2 text-sm">
            {history.map((m, i) => (
              <div
                key={i}
                className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}
              >
                <div
                  className={
                    m.role === 'user'
                      ? 'max-w-[85%] px-3 py-2 rounded-lg bg-blue-500 text-white whitespace-pre-wrap break-words'
                      : 'max-w-[85%] px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words'
                  }
                >
                  {m.content}
                </div>
              </div>
            ))}
            {chat.isPending && (
              <div className="flex justify-start">
                <div className="px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center gap-2">
                  <Spinner size="sm" />
                  <span className="text-xs text-gray-500 dark:text-gray-400">考え中...</span>
                </div>
              </div>
            )}
            {error && (
              <div className="text-xs text-red-500 px-1">{error}</div>
            )}
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
          className="flex gap-2 p-2 pb-20 md:pb-2 border-t border-gray-100 dark:border-gray-900"
        >
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="この記事について質問..."
            rows={1}
            className="flex-1 resize-none px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-400"
          />
          <button
            type="submit"
            disabled={chat.isPending || !input.trim()}
            className="px-3 py-2 text-sm rounded-md bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
