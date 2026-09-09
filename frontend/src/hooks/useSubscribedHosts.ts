import { useQuery } from '@tanstack/react-query';
import * as api from '../api/client';

/** はてブ取り込み時にスキップされる「購読済みサイト」の一覧（表示専用）。 */
export function useSubscribedHosts() {
  return useQuery({
    queryKey: ['subscribed-hosts'],
    queryFn: api.getSubscribedHosts,
  });
}
