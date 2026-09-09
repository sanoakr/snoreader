import { useQuery } from '@tanstack/react-query';

export interface SubscribedHosts {
  enabled: boolean;
  hosts: string[];
}

// api/client.ts の fetchJSON はモジュール非公開（export されていない）なので、
// 許可されたファイルの範囲でここに同じ挙動の最小限ヘルパーを用意する。
async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

/** はてブ取り込み時にスキップされる「購読済みサイト」の一覧（表示専用）。 */
export function useSubscribedHosts() {
  return useQuery({
    queryKey: ['subscribed-hosts'],
    queryFn: () => fetchJSON<SubscribedHosts>('/api/feeds/subscribed-hosts'),
  });
}
