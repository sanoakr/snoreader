import { useQuery } from '@tanstack/react-query';
import * as api from '../api/client';

// 5分。全フィード×全記事を突き合わせる重いエンドポイントなので、フィードや記事が
// 丸ごと変わるようなタイミングでない限り毎回叩き直す必要はない
const STALE_TIME_MS = 5 * 60 * 1000;

/**
 * はてブ取り込み時にスキップされる「購読済みサイト」の一覧（表示専用）。
 *
 * `enabled` はフィード ⚙ パネルが開いているときだけ true にする呼び出し側向け。
 * このクエリは全フィードと全記事を JOIN する重い集計なので、パネルを開いていない
 * アプリ起動時にまで無条件で実行すると 17k 件規模の DB で無駄なスキャンになる。
 */
export function useSubscribedHosts(enabled: boolean = true) {
  return useQuery({
    queryKey: ['subscribed-hosts'],
    queryFn: api.getSubscribedHosts,
    enabled,
    staleTime: STALE_TIME_MS,
  });
}
