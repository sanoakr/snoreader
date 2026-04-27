import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';
import type { ArticleFilters } from '../types';

export function useArticles(filters: ArticleFilters, offset = 0, limit = 50) {
  return useQuery({
    queryKey: ['articles', filters, offset, limit],
    queryFn: () => api.getArticles(filters, offset, limit),
  });
}

export function useUpdateArticle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: number; data: { is_read?: boolean; is_saved?: boolean } }) =>
      api.updateArticle(id, data),
    onSuccess: (_result, { id }) => {
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['feeds'] });
      qc.invalidateQueries({ queryKey: ['article', id] });
    },
  });
}

export function useMarkAllRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (feedId?: number) => api.markAllRead(feedId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['articles'] });
      qc.invalidateQueries({ queryKey: ['feeds'] });
    },
  });
}


export function useSummarizeArticle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.summarizeArticle(id),
    onSuccess: (data) => {
      qc.setQueryData(['article', data.id], data);
    },
  });
}

export function useSuggestTags() {
  return useMutation({
    mutationFn: (id: number) => api.suggestTags(id),
  });
}

export function useAiStatus() {
  return useQuery({
    queryKey: ['ai-status'],
    queryFn: api.getAiStatus,
    staleTime: 60_000,
  });
}

export function useSearchArticles(q: string, filters: { feed_id?: number; is_saved?: boolean } = {}, offset = 0) {
  return useQuery({
    queryKey: ['search', q, filters, offset],
    queryFn: () => api.searchArticles(q, filters, offset),
    enabled: q.length > 0,
  });
}
