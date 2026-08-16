import { useEffect, useRef, useState } from 'react';
import { useChatSuggestions, useChatWithArticle, useGenerateChatSuggestions, useRefreshChatSuggestions } from '../../hooks/useArticles';
import { Spinner } from '../common/Spinner';
import type { ChatMessage, ChatSource } from '../../types';

interface Props {
  articleId: number;
}

interface AssistantEntry extends ChatMessage {
  role: 'assistant';
  sources?: ChatSource[];
}

interface UserEntry extends ChatMessage {
  role: 'user';
}

type Entry = UserEntry | AssistantEntry;

// バックエンドの needs_web_search と同じロジック。スピナーラベル切替用。
const NOW_PATTERN = /(今何|今の|今は|今日|今週|今年|today|right now).{0,30}[?？]/i;
function looksLikeSearch(msg: string): boolean {
  const lower = msg.toLowerCase();
  if (['検索', '調べて', 'search'].some(t => lower.includes(t))) return true;
  if (['最新', 'latest'].some(t => lower.includes(t))) return true;
  return NOW_PATTERN.test(msg);
}

export function ArticleChatPanel({ articleId }: Props) {
  const [history, setHistory] = useState<Entry[]>([]);
  const [input, setInput] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pendingIsSearch, setPendingIsSearch] = useState(false);
  // 会話を踏まえて差し替えた候補。null のあいだは記事単位のキャッシュを出す
  const [followupQuestions, setFollowupQuestions] = useState<string[] | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const chat = useChatWithArticle();
  const suggestions = useChatSuggestions(articleId);
  const generateSuggestions = useGenerateChatSuggestions();
  const refreshSuggestions = useRefreshChatSuggestions();

  useEffect(() => {
    setHistory([]);
    setInput('');
    setError(null);
    setPendingIsSearch(false);
    setFollowupQuestions(null);
  }, [articleId]);

  useEffect(() => {
    historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: 'smooth' });
  }, [history.length, chat.isPending]);

  // override は質問候補チップから直接送るときに渡す。
  // 候補を作り直すのはチップ経由のときだけ（手入力では今の候補を出したままにする）
  const send = (override?: string) => {
    const fromChip = override !== undefined;
    const message = (override ?? input).trim();
    if (!message || chat.isPending) return;
    const userMsg: UserEntry = { role: 'user', content: message };
    const nextHistory: Entry[] = [...history, userMsg];
    const priorHistory: ChatMessage[] = history.map(h => ({ role: h.role, content: h.content }));
    setHistory(nextHistory);
    setInput('');
    setError(null);
    setPendingIsSearch(looksLikeSearch(message));
    chat.mutate(
      { id: articleId, message, history: priorHistory },
      {
        onSuccess: (res) => {
          const assistant: AssistantEntry = {
            role: 'assistant',
            content: res.message,
            sources: res.search_used ? res.sources : undefined,
          };
          const doneHistory: Entry[] = [...nextHistory, assistant];
          setHistory(doneHistory);
          setPendingIsSearch(false);
          if (fromChip) {
            refreshSuggestions.mutate(
              {
                id: articleId,
                history: doneHistory.map(h => ({ role: h.role, content: h.content })),
              },
              // 失敗しても今の候補を出したままにする。回答は既に出ているので、
              // ここでエラーを見せても操作の妨げになるだけ
              { onSuccess: (res) => setFollowupQuestions(res.questions) },
            );
          }
        },
        onError: (e) => {
          setHistory(history);
          setInput(message);
          setError(e instanceof Error ? e.message : 'Chat failed');
          setPendingIsSearch(false);
        },
      },
    );
  };

  const questions = followupQuestions ?? suggestions.data?.questions ?? [];
  // 生成ミューテーションは記事をまたいで生き残るので、この記事の分だけを見る
  const genForThisArticle = generateSuggestions.variables === articleId;
  const genPending = generateSuggestions.isPending && genForThisArticle;
  const genError = generateSuggestions.isError && genForThisArticle;
  const refreshPending =
    refreshSuggestions.isPending && refreshSuggestions.variables?.id === articleId;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="sticky bottom-0 bg-white dark:bg-gray-950 border-t border-gray-200 dark:border-gray-800 overflow-hidden">
      <div className="max-w-3xl mx-auto">
        {(history.length > 0 || chat.isPending || error) && (
          <div ref={historyRef} className="max-h-[20vh] overflow-y-auto p-3 space-y-2 text-sm">
            {history.map((m, i) => (
              <div key={i} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start flex-col items-start'}>
                <div
                  className={
                    m.role === 'user'
                      ? 'max-w-[85%] px-3 py-2 rounded-lg bg-blue-500 text-white whitespace-pre-wrap break-words'
                      : 'max-w-[85%] px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words'
                  }
                >
                  {m.content}
                </div>
                {m.role === 'assistant' && m.sources && m.sources.length > 0 && (
                  <details className="mt-1 max-w-[85%] text-xs text-gray-500 dark:text-gray-400">
                    <summary className="cursor-pointer hover:text-gray-700 dark:hover:text-gray-300">
                      🔍 Web 検索結果を参照 ({m.sources.length})
                    </summary>
                    <ul className="mt-1 space-y-0.5 pl-2">
                      {m.sources.map((s, si) => (
                        <li key={si}>
                          [{si + 1}]{' '}
                          <a
                            href={s.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-500 hover:underline break-all"
                          >
                            {s.title || s.url}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            ))}
            {chat.isPending && (
              <div className="flex justify-start">
                <div className="px-3 py-2 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center gap-2">
                  <Spinner size="sm" />
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    {pendingIsSearch ? 'Web 検索中...' : '考え中...'}
                  </span>
                </div>
              </div>
            )}
            {error && (
              <div className="text-xs text-red-500 px-1">{error}</div>
            )}
          </div>
        )}
        {!chat.isPending && (
          <div className="flex flex-wrap items-center gap-1.5 px-2 pt-2">
            {questions.length > 0 ? (
              <>
                {questions.map((q, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => send(q)}
                    className="px-2.5 py-1 text-xs rounded-full border border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 hover:border-blue-400 dark:hover:border-blue-500 transition-colors"
                  >
                    {q}
                  </button>
                ))}
                {/* 生成中も古いチップは消さない（消すと行が空いて画面がガタつく） */}
                {refreshPending && (
                  <span className="flex items-center gap-1.5 px-1 text-xs text-gray-400 dark:text-gray-500">
                    <Spinner size="sm" />
                    候補を更新中...
                  </span>
                )}
              </>
            ) : suggestions.isLoading ? null : (
              <>
                <button
                  type="button"
                  onClick={() => generateSuggestions.mutate(articleId)}
                  disabled={genPending}
                  className="px-2.5 py-1 text-xs rounded-full border border-dashed border-gray-300 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5"
                >
                  {genPending ? (
                    <>
                      <Spinner size="sm" />
                      候補を考え中...
                    </>
                  ) : (
                    '✨ 質問候補を生成'
                  )}
                </button>
                {genError && (
                  <span className="text-xs text-red-500">候補を生成できませんでした</span>
                )}
              </>
            )}
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
          className="flex gap-2 p-2 border-t border-gray-100 dark:border-gray-900"
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
            className="shrink-0 px-3 py-2 text-sm rounded-md bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
